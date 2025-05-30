# TALON: Techonology-Agnostic Long Read Analysis Pipeline
# Author: Dana Wyman
# -----------------------------------------------------------------------------
# This program takes transcripts from one or more samples (SAM format) and
# assigns them transcript and gene identifiers based on a GTF annotation.
# Novel transcripts are assigned new identifiers.
import argparse
import logging
import multiprocessing as mp
import operator
import os
import queue
import sqlite3
import sys
import time
import warnings
from datetime import datetime, timedelta
from functools import reduce
from itertools import islice, repeat
from pathlib import Path
from string import Template

import pandas as pd
import pysam

from talon.post import get_read_annotations

from . import dstruct
from . import init_refs as init_refs
from . import logger as logger
from . import process_sams as procsams
from . import query_utils as qutils
from . import transcript_utils as tutils

# set verbosity for pysam
save = pysam.set_verbosity(0)
# pysam.set_verbosity(save)


class Counter(object):
    def __init__(self, initval=0):
        self.val = mp.Value("i", initval)
        self.lock = mp.Lock()

    def increment(self):
        with self.lock:
            self.val.value += 1
            return self.val.value

    def value(self):
        with self.lock:
            return self.val.value


def get_counters(database):
    """Fetch counter values from the database and create counter objects
    that will be accessible to all of the threads during the parallel run
    """

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch counter values
        cursor.execute("SELECT * FROM counters WHERE category == 'genes'")
        global gene_counter
        gene_counter = Counter(initval=cursor.fetchone()["count"])

        cursor.execute("SELECT * FROM counters WHERE category == 'transcripts'")
        global transcript_counter
        transcript_counter = Counter(initval=cursor.fetchone()["count"])

        cursor.execute("SELECT * FROM counters WHERE category == 'vertex'")
        global vertex_counter
        vertex_counter = Counter(initval=cursor.fetchone()["count"])

        cursor.execute("SELECT * FROM counters WHERE category == 'edge'")
        global edge_counter
        edge_counter = Counter(initval=cursor.fetchone()["count"])

        cursor.execute("SELECT * FROM counters WHERE category == 'observed'")
        global observed_counter
        observed_counter = Counter(initval=cursor.fetchone()["count"])

        cursor.execute("SELECT * FROM counters WHERE category == 'dataset'")
        global dataset_counter
        dataset_counter = Counter(initval=cursor.fetchone()["count"])

    return


def get_args():
    """Fetches the arguments for the program"""

    program_desc = """TALON takes transcripts from one or more long read
                      datasets (SAM format) and assigns them transcript and gene
                      identifiers based on a database-bound annotation.
                      Novel events are assigned new identifiers."""
    parser = argparse.ArgumentParser(description=program_desc)

    parser.add_argument(
        "--f",
        dest="config_file",
        help="Dataset config file: dataset name, sample description, " + "platform, sam file (comma-delimited)",
        type=str,
    )
    parser.add_argument(
        "--cb",
        dest="use_cb_tag",
        action="store_true",
        help="Use CB tag in input SAM file instead of including " + "a dataset name in your config file",
        default=False,
    )
    parser.add_argument(
        "--db",
        dest="database",
        metavar="FILE,",
        type=str,
        help="TALON database. Created using talon_initialize_database",
    )
    parser.add_argument(
        "--build",
        dest="build",
        metavar="STRING,",
        type=str,
        help="Genome build (i.e. hg38) to use. Must be in the database.",
    )
    parser.add_argument(
        "--threads", "-t", dest="threads", help="Number of threads to run program with.", type=int, default=2
    )
    parser.add_argument(
        "--cov",
        "-c",
        dest="min_coverage",
        help="Minimum alignment coverage in order to use a SAM entry. Default = 0.9",
        type=float,
        default=0.9,
    )
    parser.add_argument(
        "--identity",
        "-i",
        dest="min_identity",
        help="Minimum alignment identity in order to use a SAM entry. Default = 0.8",
        type=float,
        default=0.8,
    )
    parser.add_argument(
        "--nsg",
        "--create_novel_spliced_genes",
        dest="create_novel_spliced_genes",
        action="store_true",
        help="Make novel genes with the intergenic novelty label "
        + "for transcripts that don't share "
        + "splice junctions with any other models",
        default=False,
    )
    parser.add_argument(
        "--tmpDir",
        dest="tmp_dir",
        help="Path to directory for tmp files. Default = `talon_tmp/`",
        type=str,
        default="talon_tmp/",
    )
    parser.add_argument(
        "--verbosity", "-v", type=int, default=1, help="Verbosity of TALON output. Higher numbers = more verbose."
    )
    parser.add_argument("--o", dest="outprefix", help="Prefix for output files", type=str)

    args = parser.parse_args()
    return args


def str_wrap_double(s):
    """Adds double quotes around the input string"""
    s = str(s)
    return '"' + s + '"'


def search_for_vertex_at_pos(chromosome, position, location_dict):
    """Given a chromosome and a position (1-based), this function queries the
    location dict to determine whether a vertex
    fitting those criteria exists. Returns the row if yes, and __ if no.
    """
    try:
        return location_dict[chromosome][position]
    except:
        return None


def search_for_edge(vertex_1, vertex_2, edge_type, edge_dict):
    """Search the edge dict for an edge linking vertex_1 and vertex_2"""
    query_key = (vertex_1, vertex_2, edge_type)
    try:
        return edge_dict[query_key]
    except:
        return None


def match_monoexon_vertices(chromosome, positions, strand, location_dict, run_info):
    """Given the start and end of a single-exon transcript, this function looks
    for a matching vertex for each position. Also returns a list where each
    index indicates whether that vertex is novel to the data structure
    (0 for known, 1 for novel)"""

    # Returned by function
    vertex_matches = []
    novelty = []
    diff_5p = None
    diff_3p = None

    # helpers
    start = 0
    end = len(positions) - 1

    # Iterate over positions
    for curr_index in range(0, len(positions)):
        position = positions[curr_index]

        # Start and ends require permissive matching approach
        if curr_index == start:
            sj_pos = positions[curr_index + 1]
            pos_type = "start"
            vertex_match, diff_5p = permissive_vertex_search(
                chromosome, position, strand, sj_pos, pos_type, location_dict, run_info
            )
        elif curr_index == end:
            sj_pos = positions[curr_index - 1]
            pos_type = "end"
            vertex_match, diff_3p = permissive_vertex_search(
                chromosome, position, strand, sj_pos, pos_type, location_dict, run_info
            )

        if vertex_match == None:
            # If no vertex matches the position, one is created.
            vertex_match = create_vertex(chromosome, position, location_dict, run_info)["location_ID"]
            novelty.append(1)
        else:
            novelty.append(0)

        # Add to running list of matches
        vertex_matches.append(vertex_match)

    return tuple(vertex_matches), tuple(novelty), diff_5p, diff_3p


def match_splice_vertices(chromosome, positions, strand, location_dict, run_info):
    """Given a chromosome and a list of positions from the transcript in 5' to
    3' end order, this function looks for a matching vertex for each splice
    junction position (so it ignores the ends). Also returns a list where
    each index indicates whether that vertex is novel to the data structure
    (0 for known, 1 for novel)"""

    # Returned by function
    vertex_matches = []
    novelty = []

    # Iterate over positions
    for curr_index in range(1, len(positions) - 1):
        position = positions[curr_index]

        vertex_match = search_for_vertex_at_pos(chromosome, position, location_dict)
        if vertex_match == None:
            # If no vertex matches the position, one is created.
            vertex_match = create_vertex(chromosome, position, location_dict, run_info)
            novelty.append(1)
        else:
            novelty.append(0)

        # Add to running list of matches
        vertex_matches.append(vertex_match["location_ID"])

    return vertex_matches, novelty


def match_all_transcript_vertices(chromosome, positions, strand, location_dict, run_info):
    """Given a chromosome and a list of positions from the transcript in 5' to
    3' end order, this function looks for a matching vertex for each
    position. Also returns a list where each index indicates whether that
    vertex is novel to the data structure (0 for known, 1 for novel)"""

    # Returned by function
    vertex_matches = []
    novelty = []
    diff_5p = None
    diff_3p = None

    # helpers
    start = 0
    end = len(positions) - 1

    # Iterate over positions
    for curr_index in range(0, len(positions)):
        position = positions[curr_index]

        # Start and ends require permissive matching approach
        if curr_index == start:
            sj_pos = positions[curr_index + 1]
            pos_type = "start"
            vertex_match, diff_5p = permissive_vertex_search(
                chromosome, position, strand, sj_pos, pos_type, location_dict, run_info
            )
        elif curr_index == end:
            sj_pos = positions[curr_index - 1]
            pos_type = "end"
            vertex_match, diff_3p = permissive_vertex_search(
                chromosome, position, strand, sj_pos, pos_type, location_dict, run_info
            )

        # Remaining mid-transcript positions go through strict matching process
        else:
            vertex_match = search_for_vertex_at_pos(chromosome, position, location_dict)
        if vertex_match == None:
            # If no vertex matches the position, one is created.
            vertex_match = create_vertex(chromosome, position, location_dict, run_info)
            novelty.append(1)
        else:
            novelty.append(0)

        # Add to running list of matches
        vertex_matches.append(vertex_match["location_ID"])

    return tuple(vertex_matches), tuple(novelty), diff_5p, diff_3p


def permissive_match_with_gene_priority(
    chromosome, position, strand, sj_pos, pos_type, gene_ID, gene_locs, locations, run_info
):
    """Tries to match a position to a known start/end vertex from the same
    gene. If none is found, the normal permissive match procedure is
    invoked.
    """
    # Check inputs
    if pos_type != "start" and pos_type != "end":
        msg = "Please set pos_type to either 'start' or 'end'."
        logging.error(msg)
        raise ValueError(msg)
    if strand != "+" and strand != "-":
        msg = f"Invalid strand specified: {strand}"
        logging.error(msg)
        raise ValueError(msg)

    # Try exact match first
    if chromosome in locations and position in locations[chromosome]:
        match = locations[chromosome][position]
        dist = 0
        if gene_ID in gene_locs:
            if position in gene_locs[gene_ID]:
                return match["location_ID"], dist, 1
            else:
                return match["location_ID"], dist, 0
        else:
            return match["location_ID"], dist, 0

    # This approach only works when there are known starts/ends for this gene
    if gene_ID in gene_locs:
        # Get cutoff distance
        if pos_type == "start":
            max_dist = run_info.cutoff_5p
        else:
            max_dist = run_info.cutoff_3p

        if (strand == "+" and pos_type == "start") or (strand == "-" and pos_type == "end"):
            search_window_start = position - max_dist
            search_window_end = sj_pos
        else:
            search_window_start = sj_pos
            search_window_end = position + max_dist

        # Compute distance to all of the gene positions on file
        min_abs_dist = max_dist + 1
        best_dist = None
        closest_vertex = None
        for known_location in gene_locs[gene_ID]:
            if known_location < search_window_start or known_location > search_window_end:
                continue

            curr_dist = compute_delta(known_location, position, strand)
            if abs(curr_dist) < min_abs_dist:
                best_dist = curr_dist
                min_abs_dist = abs(curr_dist)
                closest_vertex = gene_locs[gene_ID][known_location]

        # If a valid match is found, return it
        if min_abs_dist <= max_dist:
            return closest_vertex, best_dist, 1

    # Otherwise, revert to permissive match approach.
    match, dist = permissive_vertex_search(chromosome, position, strand, sj_pos, pos_type, locations, run_info)
    return match, dist, 0


def permissive_vertex_search(chromosome, position, strand, sj_pos, pos_type, locations, run_info):
    """Given a position, this function tries to find a vertex match within the
    cutoff distance that also comes before the splice junction begins.
    If no vertex is found, the function returns None."""

    # Try a strict match first
    if chromosome in locations and position in locations[chromosome]:
        match = locations[chromosome][position]
        dist = 0
        return match["location_ID"], dist

    if pos_type != "start" and pos_type != "end":
        msg = "Please set pos_type to either 'start' or 'end'."
        logging.error(msg)
        raise ValueError(msg)
    if strand != "+" and strand != "-":
        msg = f"Invalid strand specified: {s}"
        logging.error(msg)
        raise ValueError(msg)

    # If there is no strict match, look for vertices that are
    #     (1) On the correct chromosome
    #     (2) Are not located at or past the adjoining splice junction
    #     (3) Are within the permitted cutoff distance

    if pos_type == "start":
        max_dist = run_info.cutoff_5p
    else:
        max_dist = run_info.cutoff_3p

    # For + strand start and - strand end, look for a vertex with a smaller
    # position first (since degradtion is more biologically likely).
    # For the + strand, this would be a negative delta, and for the - strand,
    # it would be a positive delta
    if (strand == "+" and pos_type == "start") or (strand == "-" and pos_type == "end"):
        direction_priority = -1
        search_window_start = position - max_dist
        search_window_end = sj_pos
    else:
        direction_priority = 1
        search_window_start = sj_pos
        search_window_end = position + max_dist

    for dist in range(1, max_dist):
        curr_pos = position + dist * direction_priority
        if curr_pos > search_window_start and curr_pos < search_window_end:
            match = search_for_vertex_at_pos(chromosome, curr_pos, locations)
            if match != None:
                dist = compute_delta(curr_pos, position, strand)
                return match["location_ID"], dist

        curr_pos = position - dist * direction_priority
        if curr_pos > search_window_start and curr_pos < search_window_end:
            match = search_for_vertex_at_pos(chromosome, curr_pos, locations)
            if match != None:
                dist = compute_delta(curr_pos, position, strand)
                return match["location_ID"], dist

    return None, None


def create_vertex(chromosome, position, location_dict, run_info):
    """Creates a novel vertex and adds it to the location data structure."""
    new_ID = vertex_counter.increment()
    new_vertex = {"location_ID": new_ID, "genome_build": run_info.build, "chromosome": chromosome, "position": position}

    try:
        location_dict[chromosome][position] = new_vertex
    except:
        location_dict[chromosome] = {position: new_vertex}

    return new_vertex


def create_edge(vertex_1, vertex_2, edge_type, strand, edge_dict):
    """Creates a novel edge and adds it to the edge data structure."""
    new_ID = edge_counter.increment()
    new_edge = {"edge_ID": new_ID, "v1": vertex_1, "v2": vertex_2, "edge_type": edge_type, "strand": strand}
    edge_dict[(vertex_1, vertex_2, edge_type)] = new_edge

    return new_edge


def create_gene(chromosome, start, end, strand, memory_cursor, tmp_gene):
    """Create a novel gene and add it to the temporary table."""
    new_ID = gene_counter.increment()
    logging.debug(f'Creating new gene with id {new_ID}')


    new_gene = (new_ID, chromosome, min(start, end), max(start, end), strand)
    cols = ' ("gene_ID", "chromosome", "start", "end", "strand")'
    command = "INSERT INTO " + tmp_gene + cols + " VALUES " + "(?,?,?,?,?)"
    memory_cursor.execute(command, new_gene)
    return new_ID


def create_transcript(
    strand, chromosome, start_pos, end_pos, gene_ID, edge_IDs, vertex_IDs, transcript_dict, tmp_t, memory_cursor
):
    """Creates a novel transcript, add it to the transcript data structure,
    and add to tmp_t
    """
    # print("creating new transcript")
    new_ID = transcript_counter.increment()
    # print(f"new tid:{new_ID}")
    logging.debug(f'Creating new transcript with id {new_ID}')

    # updating the dict
    if len(edge_IDs) > 1:
        jn_path = ",".join(map(str, edge_IDs[1:-1]))
    else:
        jn_path = None

    new_transcript = {
        "transcript_ID": new_ID,
        "gene_ID": gene_ID,
        "jn_path": jn_path,
        "start_exon": edge_IDs[0],
        "end_exon": edge_IDs[-1],
        "start_vertex": vertex_IDs[0],
        "end_vertex": vertex_IDs[-1],
        "n_exons": int((len(edge_IDs) + 1) / 2),
        "chromosome": chromosome,
        "start_pos": start_pos,
        "end_pos": end_pos,
    }

    path_key = frozenset(edge_IDs)
    transcript_dict[path_key] = new_transcript

    # updating tmp_t
    new_t = (gene_ID, new_ID, chromosome, strand, min(start_pos, end_pos), max(start_pos, end_pos))
    cols = ' ("gene_ID", "transcript_ID", "chromosome", "strand", "min_pos", "max_pos")'
    command = "INSERT INTO " + tmp_t + cols + " VALUES " + "(?,?,?,?,?,?)"
    memory_cursor.execute(command, new_t)

    return new_transcript


def check_all_exons_known(novelty):
    """Given a list in which each element represents the novelty (1) or
    known-ness of a transcript edge (0), determine whether all of the
    exons are known or not. Return True if all are known, and False
    otherwise. Input should not include first or last exon."""


    if len(novelty) == 1:
        return novelty[0] == 0

    exons = novelty[1::2]

    if sum(exons) > 0:
        return False
    else:
        return True

def check_all_exons_novel(novelty):
    """Given a list in which each element represents the novelty (1) or
    known-ness of a transcript edge (0), determine whether all of the
    exons are novel or not. Return True if all are novel, and False
    otherwise. Input should not include first or last exon."""

    if len(novelty) == 1:
        return 0 # we have no exons to analyze

    exons = novelty[1::2]

    if sum(exons) != len(exons):
        return False
    else:
        return True


def check_all_SJs_known(novelty):
    """Given a list in which each element represents the novelty (1) or
    known-ness of a transcript edge (0), determine whether all of the
    introns are known or not. Return True if all are known, and False
    otherwise. Input should not include first or last exon. If there is
    only one entry, then that means there is one splice junction (two exons)"""

    if len(novelty) == 1:
        return novelty[0] == 0

    introns = novelty[::2]

    if sum(introns) > 0:
        return False
    else:
        return True


def match_all_splice_edges(vertices, strand, edge_dict, run_info):
    """Given a list of splice junction-only vertex IDs from the transcript in 5' to
    3' end order, this function looks for a matching edge ID for each
    position. If none exists, it creates one."""

    edge_matches = []
    novelty = []
    edge_type = "intron"

    for index_1 in range(0, len(vertices) - 1):
        index_2 = index_1 + 1

        if index_1 % 2 != 0:
            edge_type = "exon"
        else:
            edge_type = "intron"

        vertex_1 = vertices[index_1]
        vertex_2 = vertices[index_2]

        edge_match, curr_novelty = match_or_create_edge(vertex_1, vertex_2, edge_type, strand, edge_dict)
        edge_matches.append(edge_match)
        novelty.append(curr_novelty)

    return edge_matches, novelty


def match_or_create_edge(vertex_1, vertex_2, edge_type, strand, edge_dict):
    """Searches for edge match to provided set of vertices. If none found,
    creates a new edge."""
    novelty = 0
    edge_match = search_for_edge(vertex_1, vertex_2, edge_type, edge_dict)

    if edge_match == None:
        # If no edge matches the position, one is created.
        edge_match = create_edge(vertex_1, vertex_2, edge_type, strand, edge_dict)
        novelty = 1
    return edge_match["edge_ID"], novelty


def match_all_transcript_edges(vertices, strand, edge_dict, run_info):
    """Given a list of vertex IDs from the transcript in 5' to
    3' end order, this function looks for a matching edge ID for each
    position. If none exists, it creates one. Only used for monoexon case"""

    edge_matches = []
    novelty = []
    edge_type = "exon"

    for index_1 in range(0, len(vertices) - 1):
        index_2 = index_1 + 1

        if index_1 % 2 != 0:
            edge_type = "intron"
        else:
            edge_type = "exon"

        vertex_1 = vertices[index_1]
        vertex_2 = vertices[index_2]

        edge_match, curr_novelty = match_or_create_edge(vertex_1, vertex_2, edge_type, strand, edge_dict)
        edge_matches.append(edge_match)
        novelty.append(curr_novelty)

    return tuple(edge_matches), tuple(novelty)


def search_for_ISM(edge_IDs, transcript_dict):
    """Given a list of edges in a query transcript, determine whether it is an
    incomplete splice match (ISM) of any transcript in the dict. Will also
    return FSM matches if they're there"""

    edges = frozenset(edge_IDs)

    ISM_matches = [transcript_dict[x] for x in transcript_dict if edges.issubset(x)]

    if len(ISM_matches) > 0:
        return ISM_matches
    else:
        return None


def search_for_overlap_with_gene(chromosome, start, end, strand, cursor, run_info, tmp_gene, tmp_t, gene_IDs=None):
    """Given a start and an end value for an interval, query the database to
    determine whether the interval overlaps with any genes. If it there is
    more than one match, prioritize same-strand first and foremost.
    If there is more than one same-strand option, prioritize distance from 3' / 5'.
    Antisense matches may be returned if there is no same strand
    match.

    Parameters:
        gene_ID (list of str or None): Restrict results to genes in this list
    """
    logging.debug("Tiebreaking for gene assignment")
    min_start = min(start, end)
    max_end = max(start, end)
    query_interval = [min_start, max_end]

    if isinstance(gene_IDs, list):
        query = Template(
            """SELECT gene_ID,
                           transcript_ID,
                           chromosome,
                           min_pos,
                           max_pos,
                           strand
                    FROM $tmp_t
                    WHERE gene_ID IN $gene_ids"""
        ).substitute({"tmp_t": tmp_t, "gene_ids": qutils.format_for_IN(gene_IDs)})
    elif not gene_IDs:
        query = Template(
            """SELECT gene_ID,
                           transcript_ID,
                           chromosome,
                           min_pos,
                           max_pos,
                           strand
                    FROM $tmp_t
                    WHERE (chromosome = '$chrom') AND
                          ((min_pos <= $min_start AND max_pos >= $max_end) OR
                          (min_pos >= $min_start AND max_pos <= $max_end) OR
                          (min_pos >= $min_start AND min_pos <= $max_end) OR
                          (max_pos >= $min_start AND max_pos <= $max_end))
                     GROUP BY gene_ID;"""
        ).substitute({"tmp_t": tmp_t, "chrom": chromosome, "min_start": min_start, "max_end": max_end})
    cursor.execute(query)
    matches = cursor.fetchall()

    # restrict to just the genes we care about
    if gene_IDs:
        # print(f'restricting just to {gene_IDs}')
        logging.debug(f"Restricting gene tiebreak to {gene_IDs}")
        matches = [match for match in matches if match["gene_ID"] in gene_IDs]

    if len(matches) == 0:
        # print('herere here')
        logging.debug(f"Unable to tiebreak")
        return None, None

    # Among multiple matches, preferentially return the same-strand gene with
    # the greatest amount of overlap
    same_strand_matches = len([x for x in matches if x["strand"] == strand])

    if strand == "+" and same_strand_matches > 0 or strand == "-" and same_strand_matches == 0:
        matches = [x for x in matches if x["strand"] == "+"]
        best_match = get_best_match(matches, min_start, max_end)

    else:
        matches = [x for x in matches if x["strand"] == "-"]
        best_match = get_best_match(matches, min_start, max_end)

    return best_match["gene_ID"], best_match["strand"]


def get_best_match(matches, min_end, max_end):
    """
    Get the best gene match based on distances of start and end of
    read to starts and ends from transcripts of genes. The gene with the
    lowest absolute genomic distance between 5' ends and 3' ends will win.
    """
    min_dist = sys.maxsize
    best_match = None

    # print(f'read min: {min_end}')
    # print(f'read end: {max_end}')
    logging.debug(f"Read start / end: ({min_end}, {max_end})")

    for match in matches:
        logging.debug('')
        logging.debug(f"Matching with transcripts from gene {match['gene_ID']}, transcript {match['transcript_ID']}")
        end_dist = abs(match["max_pos"] - max_end)
        start_dist = abs(match["min_pos"] - min_end)

        logging.debug(f"Transcript start / end: ({match['min_pos']}, {match['max_pos']})")
        dist = end_dist + start_dist
        logging.debug(f"Distance between read and transcript ends: {dist}")
        if dist < min_dist:
            min_dist = dist
            best_match = match

    logging.debug(f"Best gene match: {best_match['gene_ID']}")
    # print(best_match['gene_ID'])
    return best_match


# def get_best_match(matches, query_interval):
#     """ Given a set of gene matches and a query interval, return the match
#         that has the greatest amount of overlap with the query."""
#
#     print('matching based on overlap')
#     max_overlap = 0
#     max_perc_overlap = 0
#     best_match = None
#     for match in matches:
#         print(match['gene_ID'])
#         match_interval = [match['start'], match['end']]
#         overlap, perc_overlap = get_overlap(query_interval, match_interval)
#         print(overlap)
#         print(perc_overlap)
#         if overlap > max_overlap:
#             max_overlap = overlap
#             max_perc_overlap = perc_overlap
#             best_match = match
#
#     print('best match')
#     print(best_match['gene_ID'])
#     return best_match


def get_overlap(a, b):
    """Computes the amount of overlap between two intervals.
    Returns 0 if there is no overlap. The function treats the start and
    ends of each interval as inclusive, meaning that if a = b = [10, 20],
    the overlap reported would be 11, not 10.
    Args:
        a: First interval, formattted as a list (query)
        b: Second interval, formatted as a list (reference)
        perc_overlap: Percent overlap from the reference interval that the
           query interval consumed
    """
    overlap = max(0, min(a[1], b[1]) - max(a[0], b[0]) + 1)
    ref_len = abs(b[1] - b[0])
    if ref_len != 0:
        perc_overlap = (overlap / ref_len) * 100
    else:
        perc_overlap = None
    return overlap, perc_overlap


def search_for_transcript(edge_IDs, transcript_dict):
    """Given the edge IDs (in set data structure) that make up a query
    transcript, look for a match in the transcript dict.
    Return gene ID and transcript ID if found, and None if not."""

    try:
        transcript = transcript_dict[edge_IDs]
        gene_ID = transcript["gene_ID"]
        return gene_ID, transcript

    except:
        return None, None


def process_FSM(
    chrom, positions, strand, edge_IDs, vertex_IDs, all_matches, gene_starts, gene_ends, edge_dict, locations, run_info
):
    """Given a transcript, try to find an FSM match for it"""
    gene_ID = None
    transcript_ID = None
    novelty = []
    start_vertex = None
    end_vertex = None
    start_exon = None
    end_exon = None
    diff_5p = None
    diff_3p = None
    jn_path = None

    # Check if any of the matches have the same number of exons as the query.
    # Such a match should be prioritized because it's an FSM
    n_exons = int(len(positions) / 2)
    FSM_matches = [x for x in all_matches if x["n_exons"] == n_exons]

    if len(FSM_matches) == 0:
        return None, None, [], None

    else:
        transcript_match = FSM_matches[0]
        gene_ID = transcript_match["gene_ID"]
        transcript_ID = transcript_match["transcript_ID"]

        # Check whether the query's 5' and 3' ends are within range of those of
        # the match. If not, perform a permissive match assignment
        curr_5p_diff = compute_delta(transcript_match["start_pos"], positions[0], strand)
        curr_3p_diff = compute_delta(transcript_match["end_pos"], positions[-1], strand)
        # ---------------- 5' end ---------------------------------
        if abs(curr_5p_diff) <= run_info.cutoff_5p:
            start_vertex = transcript_match["start_vertex"]
            start_exon = transcript_match["start_exon"]
            diff_5p = curr_5p_diff
            start_novelty = 0
        else:
            # First get a permissively matched start vertex
            start_vertex, start_exon, start_novelty, known_start, diff_5p = process_5p(
                chrom, positions, strand, vertex_IDs, gene_ID, gene_starts, edge_dict, locations, run_info
            )
        # ---------------- 3' end ---------------------------------
        if abs(curr_3p_diff) <= run_info.cutoff_3p:
            end_vertex = transcript_match["end_vertex"]
            end_exon = transcript_match["end_exon"]
            diff_3p = curr_3p_diff
            end_novelty = 0
        else:
            # First get a permissively matched end vertex
            end_vertex, end_exon, end_novelty, known_end, diff_3p = process_3p(
                chrom, positions, strand, vertex_IDs, gene_ID, gene_ends, edge_dict, locations, run_info
            )

        edge_IDs = [start_exon] + edge_IDs + [end_exon]
        vertex_IDs = [start_vertex] + vertex_IDs + [end_vertex]

    # Package information for output
    start_end_info = {
        "start_vertex": start_vertex,
        "end_vertex": end_vertex,
        "start_exon": start_exon,
        "end_exon": end_exon,
        "diff_5p": diff_5p,
        "diff_3p": diff_3p,
        "start_novelty": start_novelty,
        "end_novelty": end_novelty,
        "vertex_IDs": vertex_IDs,
        "edge_IDs": edge_IDs,
    }

    return gene_ID, transcript_ID, novelty, start_end_info


def process_5p(chrom, positions, strand, vertex_IDs, gene_ID, gene_starts, edge_dict, locations, run_info):
    """Conduct permissive match for 5' end and return assigned vertex,
    edge, and distance"""

    # First get a permissively matched start vertex
    start_vertex, diff_5p, known_start = permissive_match_with_gene_priority(
        chrom, positions[0], strand, positions[1], "start", gene_ID, gene_starts, locations, run_info
    )
    if start_vertex == None:
        start_vertex = create_vertex(chrom, positions[0], locations, run_info)["location_ID"]

    # Then get the start exon
    start_exon, start_novelty = match_or_create_edge(start_vertex, vertex_IDs[0], "exon", strand, edge_dict)

    # If known_start == 1, the start vertex is a known startpoint of this gene.
    #  start novelty refers to the novelty of the first exon (1 if yes, 0 if no)
    return start_vertex, start_exon, start_novelty, known_start, diff_5p


def process_3p(chrom, positions, strand, vertex_IDs, gene_ID, gene_ends, edge_dict, locations, run_info):
    """Conduct permissive match for 3' end and return assigned vertex,
    edge, and distance"""

    # First get a permissively matched end vertex
    end_vertex, diff_3p, known_end = permissive_match_with_gene_priority(
        chrom, positions[-1], strand, positions[-2], "end", gene_ID, gene_ends, locations, run_info
    )
    if end_vertex == None:
        end_vertex = create_vertex(chrom, positions[-1], locations, run_info)["location_ID"]
    # Then get the end exon
    end_exon, end_novelty = match_or_create_edge(vertex_IDs[-1], end_vertex, "exon", strand, edge_dict)
    # If known_end == 1, the end vertex is a known endpoint of this gene.
    # end novelty refers to the novelty of the final exon (1 if yes, 0 if no)
    return end_vertex, end_exon, end_novelty, known_end, diff_3p


def process_ISM(
    chrom,
    positions,
    strand,
    edge_IDs,
    vertex_IDs,
    all_matches,
    transcript_dict,
    gene_starts,
    gene_ends,
    edge_dict,
    locations,
    run_info,
    cursor,
    tmp_gene,
    tmp_t,
):
    """Given a transcript, try to find an ISM match for it. If the
    best match is an ISM with known ends, that will be promoted to NIC."""

    gene_ID = None
    transcript_ID = None
    novelty = []
    start_end_info = {}
    n_exons = int(len(positions) / 2)

    ISM = []
    suffix = []
    prefix = []

    # choose gene to assign it to
    gene_matches = list(set([match["gene_ID"] for match in all_matches]))
    logging.debug(f'Genes with matching vertices: {gene_matches}')
    # print(gene_matches)

    # tie break based on distance to 5' / 3' ends
    if len(gene_matches) > 1:
        gene_ID, _ = search_for_overlap_with_gene(
            chrom, positions[0], positions[-1], strand, cursor, run_info, tmp_gene, tmp_t, gene_IDs=gene_matches
        )
        all_matches = [m for m in all_matches if m["gene_ID"] == gene_ID]
    else:
        gene_ID = all_matches[0]["gene_ID"]

    # if we didn't assign a gene ID
    if gene_ID == None:
        return None, None, [], None

    # print('edge IDs')
    # print(edge_IDs)
    # for match in all_matches:
    #     print(f"gene id:{match['gene_ID']}")
    #     print(match['jn_path'])

    # Get matches for the ends
    if n_exons > 1:
        start_vertex, start_exon, start_novelty, known_start, diff_5p = process_5p(
            chrom, positions, strand, vertex_IDs, gene_ID, gene_starts, edge_dict, locations, run_info
        )
        end_vertex, end_exon, end_novelty, known_end, diff_3p = process_3p(
            chrom, positions, strand, vertex_IDs, gene_ID, gene_ends, edge_dict, locations, run_info
        )
        # Update info
        edge_IDs = [start_exon] + edge_IDs + [end_exon]
        vertex_IDs = [start_vertex] + vertex_IDs + [end_vertex]

        start_end_info["start_vertex"] = start_vertex
        start_end_info["end_vertex"] = end_vertex
        start_end_info["start_exon"] = start_exon
        start_end_info["end_exon"] = end_exon
        start_end_info["start_novelty"] = start_novelty
        start_end_info["end_novelty"] = end_novelty
        start_end_info["diff_5p"] = diff_5p
        start_end_info["diff_3p"] = diff_3p
        start_end_info["edge_IDs"] = edge_IDs
        start_end_info["vertex_IDs"] = vertex_IDs
    else:
        known_start = 0
        known_end = 0

    # Iterate over all matches from assigned gene to characterize ISMs
    for match in all_matches:
        # Add ISM
        ISM.append(str(match["transcript_ID"]))

        # Single-exon case
        if n_exons == 1:
            if match["n_exons"] == 1:
                # Return FSM
                gene_ID = match["gene_ID"]
                transcript_ID = match["transcript_ID"]
                novelty = []
                return gene_ID, transcript_ID, novelty, start_end_info

            match_path = match["jn_path"]
            exon = str(edge_IDs[0])
            # Look for prefix
            if match_path.startswith(exon):
                prefix.append(str(match["transcript_ID"]))
            # Look for suffix
            if match_path.endswith(exon):
                suffix.append(str(match["transcript_ID"]))
                gene_ID = match["gene_ID"]
            continue

        # Multi-exon case
        edge_str = ",".join([str(x) for x in edge_IDs[1:-1]])

        # Look for prefix
        if match["jn_path"].startswith(edge_str):
            prefix.append(str(match["transcript_ID"]))

        # Look for suffix
        if match["jn_path"].endswith(edge_str):
            gene_ID = match["gene_ID"]
            suffix.append(str(match["transcript_ID"]))

    novel_transcript = create_transcript(
        strand, chrom, positions[0], positions[-1], gene_ID, edge_IDs, vertex_IDs, transcript_dict, tmp_t, cursor
    )

    transcript_ID = novel_transcript["transcript_ID"]

    ISM_str = ",".join(ISM)
    novelty.append((transcript_ID, run_info.idprefix, "TALON", "ISM_transcript", "TRUE"))
    novelty.append((transcript_ID, run_info.idprefix, "TALON", "ISM_to_IDs", ISM_str))
    if prefix != []:
        prefix_str = ",".join(prefix)
        novelty.append((transcript_ID, run_info.idprefix, "TALON", "ISM-prefix_transcript", "TRUE"))
        novelty.append((transcript_ID, run_info.idprefix, "TALON", "ISM-prefix_to_IDs", prefix_str))
    if suffix != []:
        suffix_str = ",".join(suffix)
        novelty.append((transcript_ID, run_info.idprefix, "TALON", "ISM-suffix_transcript", "TRUE"))
        novelty.append((transcript_ID, run_info.idprefix, "TALON", "ISM-suffix_to_IDs", suffix_str))

    return gene_ID, transcript_ID, novelty, start_end_info


def assign_gene(
    vertex_IDs, strand, vertex_2_gene, chrom, start, end, cursor, run_info, tmp_gene, tmp_t):
    """
    Assign a gene to a transcript. First do this on the basis of splice site
    matching. If this yields more than one gene, then choose the gene with the
    closest 5' / 3' ends. If the splice site matching returns multiple matches
    between non-overlapping genes, mark as fusion and do not assign a gene.

    Returns:
    gene_ID (str or None): Gene ID of assigned gene, None if not found
    fusion (bool): Whether read appears to come from a novel fusion gene
    """

    # first attempt to assign based on matching vertices
    gene_ID, fusion = find_gene_match_on_vertex_basis(vertex_IDs, strand, vertex_2_gene)

    # if previous function returned more than one gene that we need to tiebreak,
    # look for closest gene based on end differences, out of candidate genes
    # only if it wasn't previously labeled as fusion
    if type(gene_ID) == list and fusion == False:
        gene_ID, match_strand = search_for_overlap_with_gene(
            chrom, start, end, strand, cursor, run_info, tmp_gene, tmp_t, gene_IDs=gene_ID
        )
    return gene_ID, fusion


def process_NIC(
    chrom,
    positions,
    strand,
    edge_IDs,
    vertex_IDs,
    transcript_dict,
    gene_starts,
    gene_ends,
    edge_dict,
    locations,
    vertex_2_gene,
    run_info,
    cursor,
    tmp_gene,
    tmp_t,
):
    """For a transcript that has been determined to be novel in catalog, find
    the proper gene match (documenting fusion event if applicable). To do
    this, look up each vertex in the vertex_2_gene dict, and keep track of all
    same-strand genes."""

    start_end_info = {}
    gene_ID, fusion = assign_gene(
        vertex_IDs,
        strand,
        vertex_2_gene,
        chrom,
        positions[0],
        positions[-1],
        cursor,
        run_info,
        tmp_gene,
        tmp_t
    )

    # gene_ID, fusion = find_gene_match_on_vertex_basis(vertex_IDs,
    #                                                   strand,
    #                                                   vertex_2_gene)
    # # otherwise look for closest gene based on end differences,
    # # only if it wasn't previously labeled as fusion
    # if gene_ID == None and fusion == False:
    #   gene_ID, match_strand = search_for_overlap_with_gene(chrom, positions[0],
    #                                                        positions[-1], strand,
    #                                                        cursor, run_info, tmp_gene,
    #                                                        gene_starts, gene_ends)
    #   print('geneid from search for overlap with gene  9NIC)')
    #   print(gene_ID)

    if gene_ID == None:
        return None, None, [], None, fusion

    # Get matches for the ends
    start_vertex, start_exon, start_novelty, known_start, diff_5p = process_5p(
        chrom, positions, strand, vertex_IDs, gene_ID, gene_starts, edge_dict, locations, run_info
    )
    end_vertex, end_exon, end_novelty, known_end, diff_3p = process_3p(
        chrom, positions, strand, vertex_IDs, gene_ID, gene_ends, edge_dict, locations, run_info
    )
    # Update info
    edge_IDs = [start_exon] + edge_IDs + [end_exon]
    vertex_IDs = [start_vertex] + vertex_IDs + [end_vertex]
    start_end_info["start_vertex"] = start_vertex
    start_end_info["end_vertex"] = end_vertex
    start_end_info["start_exon"] = start_exon
    start_end_info["end_exon"] = end_exon
    start_end_info["start_novelty"] = start_novelty
    start_end_info["end_novelty"] = end_novelty
    start_end_info["diff_5p"] = diff_5p
    start_end_info["diff_3p"] = diff_3p
    start_end_info["edge_IDs"] = edge_IDs
    start_end_info["vertex_IDs"] = vertex_IDs

    # Create a new transcript of that gene
    novel_transcript = create_transcript(
        strand, chrom, positions[0], positions[-1], gene_ID, edge_IDs, vertex_IDs, transcript_dict, tmp_t, cursor
    )
    transcript_ID = novel_transcript["transcript_ID"]
    novelty = [(transcript_ID, run_info.idprefix, "TALON", "NIC_transcript", "TRUE")]
    fusion = False

    return gene_ID, transcript_ID, novelty, start_end_info, fusion


def get_vertex_2_gene_df(vertex_2_gene):
    """
    Get a DataFrame mapping each unique combination of vertex:gene_ID

    Parameters:
        vertex_2_gene (dict): Dictionary mapping each vertex ID to a list of
            gene IDs that the vertex is found in

    Returns:
        df (pandas DataFrame): DataFrame of unique vertex:gene combinations
    """
    gids = []
    vids = []
    for key, item in vertex_2_gene.items():
        for item2 in item:
            gids.append(item2[0])
            vids.append(key)
    # df = pd.DataFrame.from_dict(vertex_2_gene, orient='index')
    df = pd.DataFrame()
    df["gid"] = gids
    df["vid"] = vids
    # print(df.head())
    # print(len(df.index))
    # print(len(df.vid.unique().tolist()))
    # print(df.loc[df.vid.duplicated(keep=False)].sort_values(by='vid'))
    return df


def find_gene_match_on_vertex_basis(vertex_IDs, strand, vertex_2_gene):
    """Use vertices in a transcript to try to pinpoint the gene it belongs to.

    Parameters:
        vertex_IDs (list of int): List of vertices in the read
        strand (str): Strand of read
        vertex_2_gene (dict): Dict. w/ keys = vertices, items = gene IDs of genes
            that use this vertex

    Returns:
        gene_ID (str or None): Gene ID of matching gene, or None if novel gene
            needs to be created
        fusion (bool): Whether gene read is from might be fusion / read through
    """
    logging.debug('Attempting to assign gene based on vertex concordance')

    gene_matches = []
    n_gene_matches = []

    for vertex in vertex_IDs:
        if vertex in vertex_2_gene:
            curr_matches = vertex_2_gene[vertex]

            # enforce same strandedness
            matches = [m for m in list(curr_matches) if m[1] == strand]

            gene_matches += [x[0] for x in list(matches)]

            # how many genes have this splice site?
            n_gene_matches.append(len(matches))
    # print('curr_matches)')
    # print(curr_matches)

    df = get_vertex_2_gene_df(vertex_2_gene)

    # print('eeps epps')
    # print(vertex_IDs)
    # print(df.head())

    # how many splice sites are from each gene
    gene_tally = dict((x, gene_matches.count(x)) for x in set(gene_matches))
    # print('tally')
    # print(gene_tally)
    # print(len(gene_tally))
    # print(n_gene_matches)
    # print(' genes')
    # print(gene_matches)

    # no shared splice junctions
    if len(gene_matches) == 0:
        return None, False

    # if there is more than one splice site from two non-overlapping genes
    # we need to make a new gene
    # when there are no shared splice sites between gene hits but we did
    # hit more than one gene
    elif max(n_gene_matches) <= 1 and len(gene_tally) > 1:
        logging.debug('Found a potential fusion transcript')
        return None, True

    # if we hit more than one gene and they have overlapping sjs,
    elif len(gene_tally) > 1:
        logging.debug('Found more than one gene w/ overlapping vertices')
        # print(gene_tally)
        # print(n_gene_matches)
        return list(gene_tally.keys()), False
        # temp = df.loc[df.gid.isin(gene_matches)].copy(deep=True)
        # temp = temp.drop_duplicates()
        #
        # # get total # vertices / gene
        # temp1 = temp.groupby('gid').count().reset_index().rename({'vid': 'n_vert'}, axis=1)
        #
        # # get total # detected vertices / gene
        # temp2 = temp.loc[temp.vid.isin(vertex_IDs)].copy(deep=True)
        # temp2 = temp2.groupby('gid').count().reset_index().rename({'vid': 'n_vert_in_t'}, axis=1)
        #
        # # merge
        # temp3 = temp1.merge(temp2, on='gid')
        # print(temp3)

    # For the main assignment, pick the gene that is observed the most
    else:
        gene_ID = max(gene_tally, key=gene_tally.get)
        fusion = False
        logging.debug(f'Assigning this read to gene {gene_ID}')


    return gene_ID, fusion


def process_NNC(
    chrom,
    positions,
    strand,
    edge_IDs,
    vertex_IDs,
    transcript_dict,
    gene_starts,
    gene_ends,
    edge_dict,
    locations,
    vertex_2_gene,
    run_info,
    cursor,
    tmp_gene,
    tmp_t,
):
    """Novel not in catalog case"""

    novelty = []
    start_end_info = {}

    # # first try to assign gene based on vertex concordance
    # gene_ID, fusion = find_gene_match_on_vertex_basis(
    #     vertex_IDs, strand, vertex_2_gene)
    #
    # # otherwise look for closest gene based on end differences
    # if gene_ID == None and fusion == False:
    #     gene_ID, match_strand = search_for_overlap_with_gene(chrom, positions[0],
    #                                                          positions[-1], strand,
    #                                                          cursor, run_info, tmp_gene,
    #                                                          gene_starts, gene_ends)
    #     print('geneid from search for overlap with gene')
    #     print(gene_ID)
    gene_ID, fusion = assign_gene(
        vertex_IDs,
        strand,
        vertex_2_gene,
        chrom,
        positions[0],
        positions[-1],
        cursor,
        run_info,
        tmp_gene,
        tmp_t
    )
    # print("gene id process_nnc")
    # print(gene_ID)
    # print(fusion)

    if gene_ID == None:
        return None, None, [], None, fusion

    logging.debug(f'Assigning this read to gene {gene_ID}')

    # Get matches for the ends
    start_vertex, start_exon, start_novelty, known_start, diff_5p = process_5p(
        chrom, positions, strand, vertex_IDs, gene_ID, gene_starts, edge_dict, locations, run_info
    )
    end_vertex, end_exon, end_novelty, known_end, diff_3p = process_3p(
        chrom, positions, strand, vertex_IDs, gene_ID, gene_ends, edge_dict, locations, run_info
    )
    # Update info
    edge_IDs = [start_exon] + edge_IDs + [end_exon]
    vertex_IDs = [start_vertex] + vertex_IDs + [end_vertex]
    start_end_info["start_vertex"] = start_vertex
    start_end_info["end_vertex"] = end_vertex
    start_end_info["start_exon"] = start_exon
    start_end_info["end_exon"] = end_exon
    start_end_info["start_novelty"] = start_novelty
    start_end_info["end_novelty"] = end_novelty
    start_end_info["diff_5p"] = diff_5p
    start_end_info["diff_3p"] = diff_3p
    start_end_info["edge_IDs"] = edge_IDs
    start_end_info["vertex_IDs"] = vertex_IDs

    transcript_ID = create_transcript(
        strand, chrom, positions[0], positions[-1], gene_ID, edge_IDs, vertex_IDs, transcript_dict, tmp_t, cursor
    )["transcript_ID"]

    novelty.append((transcript_ID, run_info.idprefix, "TALON", "NNC_transcript", "TRUE"))
    fusion = False

    return gene_ID, transcript_ID, novelty, start_end_info, fusion


def process_spliced_antisense(
    chrom,
    positions,
    strand,
    edge_IDs,
    vertex_IDs,
    transcript_dict,
    gene_starts,
    gene_ends,
    edge_dict,
    locations,
    vertex_2_gene,
    run_info,
    cursor,
    tmp_gene,
    tmp_t,
):
    """Annotate a transcript as antisense with splice junctions"""

    gene_novelty = []
    transcript_novelty = []
    start_end_info = {}

    if strand == "+":
        anti_strand = "-"
    else:
        anti_strand = "+"
    anti_gene_ID, fusion = find_gene_match_on_vertex_basis(vertex_IDs, anti_strand, vertex_2_gene)
    if type(anti_gene_ID) == list and fusion == False:
        anti_gene_ID, match_strand = search_for_overlap_with_gene(
            chrom, positions[0], positions[-1], strand, cursor, run_info, tmp_gene, tmp_t, gene_IDs=anti_gene_ID
        )
    if anti_gene_ID == None:
        return None, None, gene_novelty, transcript_novelty, start_end_info

    # Take care of ends
    start_vertex, start_exon, start_novelty, known_start, diff_5p = process_5p(
        chrom, positions, strand, vertex_IDs, anti_gene_ID, gene_ends, edge_dict, locations, run_info
    )
    end_vertex, end_exon, end_novelty, known_end, diff_3p = process_3p(
        chrom, positions, strand, vertex_IDs, anti_gene_ID, gene_starts, edge_dict, locations, run_info
    )
    # Update info
    edge_IDs = [start_exon] + edge_IDs + [end_exon]
    vertex_IDs = [start_vertex] + vertex_IDs + [end_vertex]
    start_end_info["start_vertex"] = start_vertex
    start_end_info["end_vertex"] = end_vertex
    start_end_info["start_exon"] = start_exon
    start_end_info["end_exon"] = end_exon
    start_end_info["start_novelty"] = start_novelty
    start_end_info["end_novelty"] = end_novelty
    start_end_info["diff_5p"] = diff_5p
    start_end_info["diff_3p"] = diff_3p
    start_end_info["edge_IDs"] = edge_IDs
    start_end_info["vertex_IDs"] = vertex_IDs

    gene_ID = create_gene(chrom, positions[0], positions[-1], strand, cursor, tmp_gene)
    transcript_ID = create_transcript(
        strand, chrom, positions[0], positions[-1], gene_ID, edge_IDs, vertex_IDs, transcript_dict, tmp_t, cursor
    )["transcript_ID"]

    # Handle gene annotations
    gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "antisense_gene", "TRUE"))
    gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "gene_antisense_to_IDs", anti_gene_ID))

    # Handle transcript annotations
    transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "antisense_transcript", "TRUE"))

    return gene_ID, transcript_ID, gene_novelty, transcript_novelty, start_end_info


def process_remaining_mult_cases(
    chrom,
    positions,
    strand,
    edge_IDs,
    vertex_IDs,
    transcript_dict,
    gene_starts,
    gene_ends,
    edge_dict,
    locations,
    vertex_2_gene,
    run_info,
    cursor,
    tmp_gene,
    tmp_t,
    fusion,
):
    """This function is a catch-all for multiexonic transcripts that were not
    FSM, ISM, NIC, NNC, or spliced antisense.
    """
    gene_novelty = []
    transcript_novelty = []
    start_end_info = {}
    if not run_info.create_novel_spliced_genes or not fusion:
        gene_ID, match_strand = search_for_overlap_with_gene(
            chrom, positions[0], positions[-1], strand, cursor, run_info, tmp_gene, tmp_t
        )
    else:
        gene_ID = None
        match_strand = None

    # We don't care about the gene when making these assignments
    start_vertex, start_exon, start_novelty, known_start, diff_5p = process_5p(
        chrom, positions, strand, vertex_IDs, gene_ID, gene_starts, edge_dict, locations, run_info
    )
    end_vertex, end_exon, end_novelty, known_end, diff_3p = process_3p(
        chrom, positions, strand, vertex_IDs, gene_ID, gene_ends, edge_dict, locations, run_info
    )
    # Update info
    edge_IDs = [start_exon] + edge_IDs + [end_exon]
    vertex_IDs = [start_vertex] + vertex_IDs + [end_vertex]
    start_end_info["start_vertex"] = start_vertex
    start_end_info["end_vertex"] = end_vertex
    start_end_info["start_exon"] = start_exon
    start_end_info["end_exon"] = end_exon
    start_end_info["start_novelty"] = start_novelty
    start_end_info["end_novelty"] = end_novelty
    start_end_info["diff_5p"] = diff_5p
    start_end_info["diff_3p"] = diff_3p
    start_end_info["edge_IDs"] = edge_IDs
    start_end_info["vertex_IDs"] = vertex_IDs

    if gene_ID == None:
        logging.debug(f"Fusion: {fusion}")
        if fusion:
            # print("i should be here")
            t_nov = "fusion_transcript"
            g_nov = "fusion_novel"
        else:
            # print("but I think im going here")
            t_nov = "intergenic_transcript"
            g_nov = "intergenic_novel"

        gene_ID = create_gene(chrom, positions[0], positions[-1], strand, cursor, tmp_gene)

        gene_novelty.append((gene_ID, run_info.idprefix, "TALON", g_nov, "TRUE"))

        transcript_ID = create_transcript(
            strand, chrom, positions[0], positions[-1], gene_ID, edge_IDs, vertex_IDs, transcript_dict, tmp_t, cursor
        )["transcript_ID"]
        transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", t_nov, "TRUE"))

    elif match_strand != strand:
        anti_gene_ID = gene_ID
        gene_ID = create_gene(chrom, positions[0], positions[-1], strand, cursor, tmp_gene)
        transcript_ID = create_transcript(
            strand, chrom, positions[0], positions[-1], gene_ID, edge_IDs, vertex_IDs, transcript_dict, tmp_t, cursor
        )["transcript_ID"]

        gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "antisense_gene", "TRUE"))
        gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "gene_antisense_to_IDs", anti_gene_ID))
        transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "antisense_transcript", "TRUE"))
    else:
        transcript_ID = create_transcript(
            strand, chrom, positions[0], positions[-1], gene_ID, edge_IDs, vertex_IDs, transcript_dict, tmp_t, cursor
        )["transcript_ID"]
        transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "genomic_transcript", "TRUE"))

    return gene_ID, transcript_ID, gene_novelty, transcript_novelty, start_end_info


def update_vertex_2_gene(gene_ID, vertex_IDs, strand, vertex_2_gene):
    """Add all vertices with gene pairings to vertex_2_gene dict"""

    for vertex in vertex_IDs:
        if vertex in vertex_2_gene:
            vertex_2_gene[vertex].add((gene_ID, strand))
        else:
            vertex_2_gene[vertex] = set()
            vertex_2_gene[vertex].add((gene_ID, strand))

    return


def identify_transcript(
    chrom,
    positions,
    strand,
    cursor,
    location_dict,
    edge_dict,
    transcript_dict,
    vertex_2_gene,
    gene_starts,
    gene_ends,
    run_info,
    tmp_gene,
    tmp_t,
):
    """Inputs:
     - Information about the query transcript
       - chromosome
       - list of positions
       - strand
     - Data structures
       - location_dict (position --> vertex)
       - edge_dict (v1_v2_edgetype --> edge)
       - transcript_dict
       - vertex_2_gene (maps vertices to the gene(s) they are part of)
       - gene_starts (maps gene IDs to known start vertices)
       - gene_ends (maps gene IDs to known end vertices)
       - run_info

    Outputs:
       - Assigned gene ID
       - Assigned transcript ID
       - gene and transcript novelty entries (to be added to database)
       - IDs of start and end vertices
       - 5' and 3' deltas from assigned start/end vertices
    """
    gene_novelty = []
    transcript_novelty = []
    n_exons = int(len(positions) / 2.0)
    gene_ID = None

    # Get vertex matches for the transcript positions
    vertex_IDs, v_novelty = match_splice_vertices(chrom, positions, strand, location_dict, run_info)
    logging.debug(f'Vertex IDs: {vertex_IDs}')
    logging.debug(f'Vertex novelties: {v_novelty}')


    # Get edge matches for transcript exons and introns based on the vertices
    edge_IDs, e_novelty = match_all_splice_edges(vertex_IDs, strand, edge_dict, run_info)
    logging.debug(f'Edge IDs: {edge_IDs}')
    logging.debug(f'Exon novelty: {e_novelty}')

    # Check novelty of exons and splice jns. This will help us categorize
    # what type of novelty the transcript has
    all_SJs_known = check_all_SJs_known(e_novelty)
    all_exons_known = check_all_exons_known(e_novelty)
    all_exons_novel = check_all_exons_novel(e_novelty)
    splice_vertices_known = (sum(v_novelty) == 0)
    # all_exons_novel = reduce(operator.mul, e_novelty, 1) == 1
    # print(f"all exons novel : {all_exons_novel}")
    logging.debug(f'All internal exons novel?: {all_exons_novel}')
    fusion = False

    # Look for FSM or ISM.
    if all_SJs_known:
        # Get all FSM/ISM matches
        all_matches = search_for_ISM(edge_IDs, transcript_dict)
        if all_matches != None:
            # Look for FSM first
            # print("looking for fsm")
            logging.debug('Looking for FSMs')
            gene_ID, transcript_ID, transcript_novelty, start_end_info = process_FSM(
                chrom,
                positions,
                strand,
                edge_IDs,
                vertex_IDs,
                all_matches,
                gene_starts,
                gene_ends,
                edge_dict,
                location_dict,
                run_info,
            )
            if gene_ID == None:
                # Now look for ISM
                # print("looking for ism")
                logging.debug('Looking for ISM')
                gene_ID, transcript_ID, transcript_novelty, start_end_info = process_ISM(
                    chrom,
                    positions,
                    strand,
                    edge_IDs,
                    vertex_IDs,
                    all_matches,
                    transcript_dict,
                    gene_starts,
                    gene_ends,
                    edge_dict,
                    location_dict,
                    run_info,
                    cursor,
                    tmp_gene,
                    tmp_t,
                )

        # Look for NIC
        if gene_ID == None:
            # print("looking for nic")
            logging.debug('Looking for NIC')
            gene_ID, transcript_ID, transcript_novelty, start_end_info, fusion = process_NIC(
                chrom,
                positions,
                strand,
                edge_IDs,
                vertex_IDs,
                transcript_dict,
                gene_starts,
                gene_ends,
                edge_dict,
                location_dict,
                vertex_2_gene,
                run_info,
                cursor,
                tmp_gene,
                tmp_t,
            )

    # Novel in catalog transcripts have known splice donors and acceptors,
    # but new connections between them.
    elif splice_vertices_known and gene_ID == None:
        # print("looking for nic (again?)")
        logging.debug('Looking for NIC (2)')
        gene_ID, transcript_ID, transcript_novelty, start_end_info, fusion = process_NIC(
            chrom,
            positions,
            strand,
            edge_IDs,
            vertex_IDs,
            transcript_dict,
            gene_starts,
            gene_ends,
            edge_dict,
            location_dict,
            vertex_2_gene,
            run_info,
            cursor,
            tmp_gene,
            tmp_t,
        )

    # Antisense transcript with splice junctions matching known gene
    if splice_vertices_known and gene_ID == None and not fusion:
        # print("looking for spliced antisese")
        logging.debug('Looking for splice antisense')
        gene_ID, transcript_ID, gene_novelty, transcript_novelty, start_end_info = process_spliced_antisense(
            chrom,
            positions,
            strand,
            edge_IDs,
            vertex_IDs,
            transcript_dict,
            gene_starts,
            gene_ends,
            edge_dict,
            location_dict,
            vertex_2_gene,
            run_info,
            cursor,
            tmp_gene,
            tmp_t,
        )

    # Novel not in catalog transcripts contain new splice donors/acceptors
    # and contain at least one splice junction. There should also be at least
    # one shared exon from existing transcripts to even try assigning a gene
    elif not (splice_vertices_known) and not fusion and not all_exons_novel:
        # print("lookign for NNCs")
        logging.debug('Looking for NNC')
        gene_ID, transcript_ID, transcript_novelty, start_end_info, fusion = process_NNC(
            chrom,
            positions,
            strand,
            edge_IDs,
            vertex_IDs,
            transcript_dict,
            gene_starts,
            gene_ends,
            edge_dict,
            location_dict,
            vertex_2_gene,
            run_info,
            cursor,
            tmp_gene,
            tmp_t,
        )
        # print(f"geneID from process_nnc: {gene_ID}")
    # Transcripts that don't match the previous categories end up here
    if gene_ID == None:
        # print("looking for this other stuff")
        logging.debug('Looking for everything else')
        gene_ID, transcript_ID, gene_novelty, transcript_novelty, start_end_info = process_remaining_mult_cases(
            chrom,
            positions,
            strand,
            edge_IDs,
            vertex_IDs,
            transcript_dict,
            gene_starts,
            gene_ends,
            edge_dict,
            location_dict,
            vertex_2_gene,
            run_info,
            cursor,
            tmp_gene,
            tmp_t,
            fusion,
        )

    logging.debug(f'Gene ID for this read: {gene_ID}')

    # Add all novel vertices to vertex_2_gene now that we have the gene ID
    vertex_IDs = start_end_info["vertex_IDs"]
    edge_IDs = start_end_info["edge_IDs"]
    e_novelty = [start_end_info["start_novelty"]] + e_novelty + [start_end_info["end_novelty"]]

    update_vertex_2_gene(gene_ID, vertex_IDs, strand, vertex_2_gene)

    # For novel genes and transcripts, add names to novelty entries
    talon_gene_name, talon_transcript_name = construct_names(
        gene_ID, transcript_ID, run_info.idprefix, run_info.n_places
    )
    if len(gene_novelty) > 0:
        gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "gene_status", "NOVEL"))
        gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "gene_name", talon_gene_name))
        gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "gene_id", talon_gene_name))
    if len(transcript_novelty) > 0:
        transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "transcript_status", "NOVEL"))
        transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "transcript_name", talon_transcript_name))
        transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "transcript_id", talon_transcript_name))
    # Add annotation entries for any novel exons
    exon_novelty = []
    exons = edge_IDs[::2]
    e_novelty = e_novelty[::2]

    if sum(e_novelty) > 0:
        for exon, is_novel in zip(exons, e_novelty):
            if is_novel:
                exon_novelty.append((exon, run_info.idprefix, "TALON", "exon_status", "NOVEL"))

    # Package up information for output
    annotations = dstruct.Struct()
    annotations.gene_ID = gene_ID
    annotations.transcript_ID = transcript_ID
    annotations.gene_novelty = gene_novelty
    annotations.transcript_novelty = transcript_novelty
    annotations.exon_novelty = exon_novelty
    annotations.start_vertex = start_end_info["start_vertex"]
    annotations.end_vertex = start_end_info["end_vertex"]
    annotations.start_exon = start_end_info["start_exon"]
    annotations.end_exon = start_end_info["end_exon"]
    annotations.start_delta = start_end_info["diff_5p"]
    annotations.end_delta = start_end_info["diff_3p"]
    return annotations


def construct_names(gene_ID, transcript_ID, prefix, n_places):
    """Create a gene and transcript name using the TALON IDs.
    The n_places variable indicates how many characters long the numeric
    part of the name should be."""

    gene_ID_str = str(gene_ID).zfill(n_places)
    gene_name = prefix + "G" + gene_ID_str

    transcript_ID_str = str(transcript_ID).zfill(n_places)
    transcript_name = prefix + "T" + transcript_ID_str

    return gene_name, transcript_name


def check_inputs(options):
    """Checks the input options provided by the user and makes sure that
    they are valid. Throw an error with descriptive help message if not."""

    # Make sure that the input database exists!
    database = options.database
    if not Path(database).exists():
        msg = f"Database file '{s}' does not exist!"
        logging.error(msg)
        raise ValueError(msg)

    # Make sure that the genome build exists in the provided TALON database.
    with sqlite3.connect(database) as conn:
        cursor = conn.cursor()
        cursor.execute(""" SELECT DISTINCT name FROM genome_build """)
        builds = [str(x[0]) for x in cursor.fetchall()]
        if options.build not in builds:
            build_names = ", ".join(list(builds))
            msg = "Please specify a genome build that exists in the" + " database. The choices are: " + build_names
            logging.error(msg)
            raise ValueError(msg)

        # Make sure that each input dataset is not already in the database, and
        # also make sure that each dataset name is unique
        if not options.use_cb_tag:
            sam_files = []
            dataset_metadata = []
            curr_datasets = []

            cursor.execute(""" SELECT dataset_name FROM dataset """)
            existing_datasets = [str(x[0]) for x in cursor.fetchall()]

            with open(options.config_file, "r") as f:
                for line in f:
                    line = line.strip().split(",")
                    curr_sam = line[3]
                    if len(line) != 4:
                        msg = (
                            "Incorrect number of comma-separated fields"
                            + " in config file. There should be four: "
                            + "(dataset name, sample description, "
                            + "platform, associated sam/bam file)."
                        )
                        logging.error(msg)
                        raise ValueError(msg)

                    # Make sure that the sam file exists
                    if not Path(curr_sam).exists():
                        msg = f"SAM/BAM file '{curr_sam}' does not exist!"
                        logging.error(msg)
                        raise ValueError(msg)

                    metadata = (line[0], line[1], line[2])
                    dataname = metadata[0]
                    if dataname in existing_datasets:
                        logging.warning(
                            "Ignoring dataset with name '" + dataname + "' because it is already in the database."
                        )
                    elif dataname in curr_datasets:
                        logging.warning("Skipping duplicated instance of dataset '" + dataname + "'.")
                    elif curr_sam in sam_files:
                        logging.warning("Skipping duplicated instance of sam file '" + curr_sam + "'.")
                    else:
                        dataset_metadata.append(metadata)
                        curr_datasets.append(dataname)
                    if not curr_sam.endswith(".sam") and not curr_sam.endswith(".bam"):
                        msg = "Last field in config file must be a .sam/.bam file"
                        logging.error(msg)
                        raise ValueError(msg)
                    sam_files.append(curr_sam)

        # if we are using the RG tag, check that the config file adheres to the
        # specifications and that the RG datasets are not already in the database
        elif options.use_cb_tag:
            sam_files = []
            dataset_metadata = []
            curr_datasets = []
            cursor.execute(""" SELECT dataset_name FROM dataset """)
            existing_datasets = [str(x[0]) for x in cursor.fetchall()]

            with open(options.config_file, "r") as f:
                n_lines = 0
                for line in f:
                    n_lines += 1

                    # # placeholder to avoid having to
                    # if n_lines > 1:
                    #     raise ValueError('Using more than one sam file with RG '+\
                    #                      'tag is currently unsupported')

                    line = line.strip().split(",")
                    curr_sam = line[2]
                    if len(line) != 3:
                        msg = (
                            "Incorrect number of comma-separated fields"
                            + " in config file. There should be three: "
                            + "(sample description, "
                            + "platform, associated sam/bam file)."
                        )
                        logging.error(msg)
                        raise ValueError(msg)

                    # Make sure that the sam file exists
                    if not Path(curr_sam).exists():
                        msg = f"SAM/BAM file '{curr_sam}' does not exist!"
                        logging.error(msg)
                        raise ValueError(msg)
                    metadata = ["", line[0], line[1]]

                    # get list of dataset names from the CB tag in the sam file
                    if curr_sam.endswith(".sam"):
                        # which rows are comment rows?
                        with open(curr_sam, "r") as infile:
                            skip_rows = []
                            for i, line in enumerate(infile):
                                if line.startswith("@"):
                                    skip_rows.append(i)
                                else:
                                    break
                        # read just the cb tags
                        df = pd.read_csv(
                            curr_sam,
                            sep="\tCB:Z:",
                            skiprows=skip_rows,
                            usecols=[1],
                            header=None,
                            names=["cb_tag"],
                            engine="python",
                        )
                        # is the df empty?
                        if df.empty:
                            msg = "SAM/BAM file contains no CB tags"
                            logging.error(msg)
                            raise RuntimeError(msg)
                        df["dataset"] = df.cb_tag.str.split(pat="\t", n=1, expand=True)[0]
                        datasets = df.dataset.unique().tolist()

                    elif curr_sam.endswith(".bam"):
                        datasets = []

                        # make an index so we can use the pysam fetch function
                        pysam.index(curr_sam)
                        infile = pysam.AlignmentFile(curr_sam, "rb")
                        for read in infile.fetch():
                            tag = read.get_tag("CB")
                            datasets.append(tag)
                        # only unique dataset
                        datasets = list(set(datasets))

                    for dataname in datasets:
                        metadata[0] = dataname

                        if dataname in existing_datasets:
                            msg = f"Dataset for read group {f} " + "already in database."
                            logging.error(msg)
                            raise RuntimeError(msg)
                            # warnings.warn("Ignoring dataset with name '" + dataname + \
                            #               "' because it is already in the database.")
                        elif dataname in curr_datasets:
                            msg = f"Dataset for read group {f} " + "already in config file."
                            logging.error(msg)
                            raise RuntimeError(msg)
                            # warnings.warn("Skipping duplicated instance of dataset '" + \
                            #                dataname + "'.")
                        else:
                            dataset_metadata.append(tuple(metadata))
                            curr_datasets.append(dataname)
                    if curr_sam in sam_files:
                        logging.warning("Skipping duplicated instance of sam/bam file '" + curr_sam + "'.")
                    if not curr_sam.endswith(".sam") and not curr_sam.endswith(".bam"):
                        msg = "Last field in config file must be a .sam/.bam file"
                        logging.error(msg)
                        raise ValueError(msg)
                    sam_files.append(curr_sam)

                    # else:
                    #     dataset_metadata.append(metadata)
                    #     curr_datasets.append(dataname)
                    #     if not curr_sam.endswith(".sam"):
                    #         raise ValueError('Last field in config file must be a .sam file')
                    #     sam_files.append(curr_sam)

    if sam_files == []:
        msg = "All of the provided dataset names are already in " + "the database. Please check your config file."
        logging.error(msg)
        raise RuntimeError(msg)

    return sam_files, dataset_metadata


def init_run_info(
    database,
    genome_build,
    min_coverage=0.9,
    min_identity=0,
    use_cb_tag=False,
    create_novel_spliced_genes=False,
    tmp_dir="talon_tmp/",
):
    """Initializes a dictionary that keeps track of important run information
    such as the desired genome build, the prefix for novel identifiers,
    and the novel counters for the run."""

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        run_info = dstruct.Struct()
        run_info.build = genome_build
        run_info.min_coverage = min_coverage
        run_info.min_identity = min_identity
        run_info.use_cb_tag = use_cb_tag
        run_info.create_novel_spliced_genes = create_novel_spliced_genes
        run_info.tmp_dir = tmp_dir
        os.system("mkdir -p %s " % (tmp_dir))

        # Fetch information from run_info table
        cursor.execute("""SELECT * FROM run_info""")
        for info in cursor.fetchall():
            info_name = info["item"]
            value = info["value"]
            if info_name not in ["idprefix", "schema_version"]:
                value = int(value)
            run_info[info_name] = value

        # Fetch dataset counter
        query = "SELECT * FROM counters WHERE category == 'dataset'"
        cursor.execute(query)
        run_info.dataset = cursor.fetchone()["count"]

    return run_info


def init_outfiles(outprefix, tmp_dir="talon_tmp/"):
    """Initialize output files for the run that all processes will be able to
    write to via the queue."""

    # If there is a tmp dir there already, remove it
    if os.path.exists(tmp_dir):
        os.system("rm -r %s" % tmp_dir)
    os.system("mkdir -p %s" % tmp_dir)

    if not tmp_dir.endswith("/"):
        tmp_dir = tmp_dir + "/"

    # Check on main outpath
    if os.path.isdir(outprefix):
        if not outprefix.endswith("/"):
            outprefix = outprefix + "/"
        # Add default prefix
        outprefix = outprefix + "talon"

    # Now initialize the files
    outfiles = dstruct.Struct()
    outfiles.qc = outprefix + "_QC.log"
    outfiles.abundance = tmp_dir + "abundance_tuples.tsv"
    outfiles.genes = tmp_dir + "gene_tuples.tsv"
    outfiles.transcripts = tmp_dir + "transcript_tuples.tsv"
    outfiles.edges = tmp_dir + "edge_tuples.tsv"
    outfiles.v2g = tmp_dir + "vertex_2_gene_tuples.tsv"
    outfiles.location = tmp_dir + "location_tuples.tsv"
    outfiles.observed = tmp_dir + "observed_transcript_tuples.tsv"
    outfiles.gene_annot = tmp_dir + "gene_annot_tuples.tsv"
    outfiles.transcript_annot = tmp_dir + "transcript_annot_tuples.tsv"
    outfiles.exon_annot = tmp_dir + "exon_annot_tuples.tsv"

    for fname in outfiles:
        # Replace with handle to open file
        open(outfiles[fname], "w").close()

    return outfiles


def prepare_data_structures(cursor, run_info, chrom=None, start=None, end=None, tmp_id="1"):
    """Initializes data structures needed for the run and organizes them
    in a dictionary for more ease of use when passing them between functions
    """
    build = run_info.build
    min_coverage = run_info.min_coverage
    min_identity = run_info.min_identity
    struct_collection = dstruct.Struct()

    struct_collection.tmp_gene = init_refs.make_temp_novel_gene_table(
        cursor, build, chrom=chrom, start=start, end=end, tmp_tab="temp_gene_" + tmp_id
    )

    struct_collection.tmp_monoexon = init_refs.make_temp_monoexonic_transcript_table(
        cursor, build, chrom=chrom, start=start, end=end, tmp_tab="temp_monoexon_" + tmp_id
    )

    struct_collection.tmp_t = init_refs.make_temp_transcript_table(
        cursor, build, chrom=chrom, start=start, end=end, tmp_tab="temp_t_" + tmp_id
    )

    location_dict = init_refs.make_location_dict(build, cursor, chrom=chrom, start=start, end=end)

    edge_dict = init_refs.make_edge_dict(cursor, build=build, chrom=chrom, start=start, end=end)

    transcript_dict = init_refs.make_transcript_dict(cursor, build, chrom=chrom, start=start, end=end)

    vertex_2_gene = init_refs.make_vertex_2_gene_dict(cursor, build=build, chrom=chrom, start=start, end=end)

    gene_starts = init_refs.make_gene_start_or_end_dict(cursor, build, "start", chrom=chrom, start=start, end=end)
    gene_ends = init_refs.make_gene_start_or_end_dict(cursor, build, "end", chrom=chrom, start=start, end=end)

    struct_collection.location_dict = location_dict
    struct_collection.edge_dict = edge_dict
    struct_collection.transcript_dict = transcript_dict
    struct_collection.vertex_2_gene = vertex_2_gene
    struct_collection.gene_starts = gene_starts
    struct_collection.gene_ends = gene_ends

    return struct_collection


def compute_delta(orig_pos, new_pos, strand):
    """Given a starting position and a new position, compute the distance
    between them. The sign indicates whether the second point is
    upstream or downstream of the original with respect to strand."""

    abs_dist = abs(orig_pos - new_pos)
    if strand == "+":
        if new_pos < orig_pos:
            return -1 * abs_dist
        else:
            return abs_dist
    elif strand == "-":
        if new_pos < orig_pos:
            return abs_dist
        else:
            return -1 * abs_dist
    else:
        msg = "Strand must be either + or -"
        logging.error(msg)
        raise ValueError(msg)


def identify_monoexon_transcript(
    chrom,
    positions,
    strand,
    cursor,
    location_dict,
    edge_dict,
    transcript_dict,
    vertex_2_gene,
    gene_starts,
    gene_ends,
    run_info,
    tmp_gene,
    tmp_t,
    tmp_monoexon,
):
    gene_novelty = []
    transcript_novelty = []
    exon_novelty = []

    cutoff_5p = run_info.cutoff_5p
    cutoff_3p = run_info.cutoff_3p

    start = positions[0]
    end = positions[-1]
    # First, look for a monoexonic transcript match that overlaps the current
    # transcript
    query = Template(
        """ SELECT *
                    FROM $tmp_monoexon AS tm
                    WHERE tm.chromosome = '$chrom'
                    AND tm.strand = '$strand'
                    AND ((min_pos <= $start AND max_pos >= $end)
                      OR (min_pos >= $start AND max_pos <= $end)
                      OR (min_pos >= $start AND min_pos <= $end)
                      OR (max_pos >= $start AND max_pos <= $end))
                    """
    ).substitute(
        {
            "tmp_monoexon": tmp_monoexon,
            "chrom": chrom,
            "strand": strand,
            "start": min(start, end),
            "end": max(start, end),
        }
    )

    cursor.execute(query)
    matches = cursor.fetchall()

    # If there is more than one match, apply a tiebreaker (pick the one with
    # the most overlap
    if len(matches) > 0:
        best_overlap = 0
        best_match = None
        for match in matches:
            # get overlap and compare
            match_interval = [match["start"], match["end"]]
            overlap, perc_overlap = get_overlap([start, end], match_interval)
            if overlap >= best_overlap:
                best_overlap = overlap
                best_match = match

        gene_ID = best_match["gene_ID"]
        transcript_ID = best_match["transcript_ID"]
        vertex_IDs = (best_match["start_vertex"], best_match["end_vertex"])
        edge_IDs = [best_match["exon_ID"]]
        diff_5p = compute_delta(best_match["start"], start, strand)
        diff_3p = compute_delta(best_match["end"], end, strand)

    # If there is no match, proceed to genomic/antisense style matching.
    else:
        # Start by performing vertex match
        vertex_IDs, v_novelty, diff_5p, diff_3p = match_monoexon_vertices(
            chrom, positions, strand, location_dict, run_info
        )

        # Get edge match (or create new edge)
        edge_IDs, e_novelty = match_all_transcript_edges(vertex_IDs, strand, edge_dict, run_info)

        # If the exon is known, then this transcript must be ISM or NIC
        gene_ID = None
        if e_novelty[0] == 0:
            all_matches = search_for_ISM(edge_IDs, transcript_dict)

            if all_matches != None:
                gene_ID, transcript_ID, transcript_novelty, info = process_ISM(
                    chrom,
                    positions,
                    strand,
                    edge_IDs,
                    vertex_IDs,
                    all_matches,
                    transcript_dict,
                    gene_starts,
                    gene_ends,
                    edge_dict,
                    location_dict,
                    run_info,
                    cursor,
                    tmp_gene,
                    tmp_t,
                )
        if gene_ID == None:
            # Find best gene match using overlap search if the ISM/NIC check didn't work
            gene_ID, match_strand = search_for_overlap_with_gene(
                chrom, positions[0], positions[1], strand, cursor, run_info, tmp_gene, tmp_t
            )
            # Intergenic case
            if gene_ID == None:
                gene_ID = create_gene(chrom, positions[0], positions[-1], strand, cursor, tmp_gene)

                gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "intergenic_novel", "TRUE"))
                transcript_ID = create_transcript(
                    strand,
                    chrom,
                    positions[0],
                    positions[-1],
                    gene_ID,
                    edge_IDs,
                    vertex_IDs,
                    transcript_dict,
                    tmp_t,
                    cursor,
                )["transcript_ID"]
                transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "intergenic_transcript", "TRUE"))
            # Antisense case
            elif match_strand != strand:
                anti_gene_ID = gene_ID
                gene_ID = create_gene(chrom, positions[0], positions[-1], strand, cursor, tmp_gene)
                transcript_ID = create_transcript(
                    strand,
                    chrom,
                    positions[0],
                    positions[-1],
                    gene_ID,
                    edge_IDs,
                    vertex_IDs,
                    transcript_dict,
                    tmp_t,
                    cursor,
                )["transcript_ID"]

                gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "antisense_gene", "TRUE"))
                gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "gene_antisense_to_IDs", anti_gene_ID))
                transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "antisense_transcript", "TRUE"))

            # Same strand
            else:
                transcript_ID = create_transcript(
                    strand,
                    chrom,
                    positions[0],
                    positions[-1],
                    gene_ID,
                    edge_IDs,
                    vertex_IDs,
                    transcript_dict,
                    tmp_t,
                    cursor,
                )["transcript_ID"]
                transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "genomic_transcript", "TRUE"))

        # Add all novel vertices to vertex_2_gene now that we have the gene ID
        update_vertex_2_gene(gene_ID, vertex_IDs, strand, vertex_2_gene)

        talon_gene_name, talon_transcript_name = construct_names(
            gene_ID, transcript_ID, run_info.idprefix, run_info.n_places
        )

        # Add novel gene annotation attributes
        if len(gene_novelty) > 0:
            gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "gene_status", "NOVEL"))
            gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "gene_name", talon_gene_name))
            gene_novelty.append((gene_ID, run_info.idprefix, "TALON", "gene_id", talon_gene_name))

        # Add novel transcript annotation attributes
        transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "transcript_status", "NOVEL"))
        transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "transcript_name", talon_transcript_name))
        transcript_novelty.append((transcript_ID, run_info.idprefix, "TALON", "transcript_id", talon_transcript_name))

        # Add annotation entries for any novel exons
        if e_novelty[0] == 1:
            exon_novelty.append((edge_IDs[0], run_info.idprefix, "TALON", "exon_status", "NOVEL"))

        # Add the novel transcript to the temporary monoexon table
        new_mono = (
            gene_ID,
            transcript_ID,
            chrom,
            start,
            end,
            strand,
            vertex_IDs[0],
            vertex_IDs[-1],
            edge_IDs[0],
            min(start, end),
            max(start, end),
        )
        cols = (
            '("gene_ID", "transcript_ID", "chromosome", "start", "end",'
            + '"strand", "start_vertex", "end_vertex", "exon_ID", "min_pos",'
            + '"max_pos")'
        )
        command = "INSERT INTO " + tmp_monoexon + " " + cols + " VALUES " + "(?,?,?,?,?,?,?,?,?,?,?)"
        cursor.execute(command, new_mono)

    # Package annotation information
    annotations = dstruct.Struct()
    annotations.gene_ID = gene_ID
    annotations.transcript_ID = transcript_ID
    annotations.gene_novelty = gene_novelty
    annotations.transcript_novelty = transcript_novelty
    annotations.exon_novelty = exon_novelty
    annotations.start_vertex = vertex_IDs[0]
    annotations.end_vertex = vertex_IDs[-1]
    annotations.start_exon = edge_IDs[0]
    annotations.end_exon = edge_IDs[-1]
    annotations.start_delta = diff_5p
    annotations.end_delta = diff_3p

    return annotations


def update_database(database, batch_size, outfiles, datasets):
    """Adds new entries to the database."""

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    batch_add_genes(cursor, outfiles.genes, batch_size)
    batch_add_transcripts(cursor, outfiles.transcripts, batch_size)
    batch_add_edges(cursor, outfiles.edges, batch_size)
    batch_add_locations(cursor, outfiles.location, batch_size)
    batch_add_vertex2gene(cursor, outfiles.v2g, batch_size)
    add_datasets(cursor, datasets)
    batch_add_observed(cursor, outfiles.observed, batch_size)
    update_counter(cursor)
    batch_add_annotations(cursor, outfiles.gene_annot, "gene", batch_size)
    batch_add_annotations(cursor, outfiles.transcript_annot, "transcript", batch_size)
    batch_add_annotations(cursor, outfiles.exon_annot, "exon", batch_size)

    check_database_integrity(cursor)
    conn.commit()
    conn.close()

    return


def update_counter(cursor):  # , n_datasets):
    """Update the database counter using the global counter variables"""

    update_g = 'UPDATE "counters" SET "count" = ? WHERE "category" = "genes"'
    cursor.execute(update_g, [gene_counter.value()])

    update_t = 'UPDATE "counters" SET "count" = ? WHERE "category" = "transcripts"'
    cursor.execute(update_t, [transcript_counter.value()])

    update_e = 'UPDATE "counters" SET "count" = ? WHERE "category" = "edge"'
    cursor.execute(update_e, [edge_counter.value()])

    update_v = 'UPDATE "counters" SET "count" = ? WHERE "category" = "vertex"'
    cursor.execute(update_v, [vertex_counter.value()])

    update_d = 'UPDATE "counters" SET "count" = ? WHERE "category" = "dataset"'
    cursor.execute(update_d, [dataset_counter.value()])
    # cursor.execute(update_d,[n_datasets])

    update_o = 'UPDATE "counters" SET "count" = ? WHERE "category" = "observed"'
    cursor.execute(update_o, [observed_counter.value()])

    return


def batch_add_vertex2gene(cursor, v2g_file, batch_size):
    """Add new vertex-gene relationships to the vertex table"""

    with open(v2g_file, "r") as f:
        while True:
            batch = [tuple(x.strip().split("\t")) for x in islice(f, batch_size)]

            if batch == []:
                break

            try:
                cols = " (" + ", ".join([str_wrap_double(x) for x in ["vertex_ID", "gene_ID"]]) + ") "
                command = 'INSERT OR IGNORE INTO "vertex"' + cols + "VALUES " + "(?,?)"
                cursor.executemany(command, batch)

            except Exception as e:
                logging.error(e)
                sys.exit(1)
    return


def batch_add_locations(cursor, location_file, batch_size):
    """Add new locations to database"""

    with open(location_file, "r") as f:
        while True:
            batch = [tuple(x.strip().split("\t")) for x in islice(f, batch_size)]

            if batch == []:
                break

            try:
                cols = (
                    " ("
                    + ", ".join([str_wrap_double(x) for x in ["location_ID", "genome_build", "chromosome", "position"]])
                    + ") "
                )
                command = 'INSERT INTO "location"' + cols + "VALUES " + "(?,?,?,?)"
                cursor.executemany(command, batch)

            except Exception as e:
                logging.error(e)
                sys.exit(1)
    return


def batch_add_edges(cursor, edge_file, batch_size):
    """Add new edges to database"""

    with open(edge_file, "r") as f:
        while True:
            batch = [tuple(x.strip().split("\t")) for x in islice(f, batch_size)]

            if batch == []:
                break

            try:
                cols = (
                    " ("
                    + ", ".join([str_wrap_double(x) for x in ["edge_ID", "v1", "v2", "edge_type", "strand"]])
                    + ") "
                )
                command = 'INSERT INTO "edge"' + cols + "VALUES " + "(?,?,?,?,?)"
                cursor.executemany(command, batch)

            except Exception as e:
                logging.error(e)
                sys.exit(1)

    return


def batch_add_transcripts(cursor, transcript_file, batch_size):
    """Add new transcripts to database"""

    with open(transcript_file, "r") as f:
        while True:
            batch_lines = islice(f, batch_size)
            batch = []
            for line in batch_lines:
                transcript = line.strip().split("\t")
                if transcript[3] == "None":
                    transcript[3] = None
                batch.append(transcript)

            if batch == []:
                break

            try:
                cols = (
                    " ("
                    + ", ".join(
                        [
                            str_wrap_double(x)
                            for x in [
                                "transcript_id",
                                "gene_id",
                                "start_exon",
                                "jn_path",
                                "end_exon",
                                "start_vertex",
                                "end_vertex",
                                "n_exons",
                            ]
                        ]
                    )
                    + ") "
                )
                command = 'INSERT INTO "transcripts"' + cols + "VALUES " + "(?,?,?,?,?,?,?,?)"
                cursor.executemany(command, batch)

            except Exception as e:
                logging.error(e)
                sys.exit(1)

    return


def batch_add_genes(cursor, gene_file, batch_size):
    """Add genes to the database gene table"""

    with open(gene_file, "r") as f:
        while True:
            batch = [tuple(x.strip().split("\t")) for x in islice(f, batch_size)]

            if batch == []:
                break

            try:
                cols = " (" + ", ".join([str_wrap_double(x) for x in ["gene_ID", "strand"]]) + ") "
                command = "INSERT OR IGNORE INTO genes" + cols + "VALUES " + "(?,?)"
                cursor.executemany(command, batch)

            except Exception as e:
                logging.error(e)
                sys.exit(1)
    return


def add_datasets(cursor, datasets):
    """Add dataset records to database"""

    try:
        cols = (
            " (" + ", ".join([str_wrap_double(x) for x in ["dataset_ID", "dataset_name", "sample", "platform"]]) + ") "
        )
        command = 'INSERT INTO "dataset"' + cols + "VALUES " + "(?,?,?,?)"
        cursor.executemany(command, datasets)

    except Exception as e:
        logging.error(e)
        sys.exit(1)
    return


def batch_add_annotations(cursor, annot_file, annot_type, batch_size):
    """Add gene/transcript/exon annotations to the appropriate annotation table"""
    batch_size = 1
    if annot_type not in ["gene", "transcript", "exon"]:
        msg = "When running batch annot update, must specify " + "annot_type as 'gene', 'exon', or 'transcript'."
        logging.error(msg)
        raise ValueError(msg)

    with open(annot_file, "r") as f:
        while True:
            batch = [tuple(x.strip().split("\t")) for x in islice(f, batch_size)]

            if batch == []:
                break

            try:
                cols = (
                    " ("
                    + ", ".join([str_wrap_double(x) for x in ["ID", "annot_name", "source", "attribute", "value"]])
                    + ") "
                )
                command = 'INSERT OR IGNORE INTO "' + annot_type + '_annotations" ' + cols + "VALUES " + "(?,?,?,?,?)"
                cursor.executemany(command, batch)

            except Exception as e:
                logging.error(e)
                sys.exit(1)
    return


def batch_add_observed(cursor, observed_file, batch_size):
    """Adds observed tuples (obs_ID, gene_ID, transcript_ID, read_name,
    dataset, start_vertex_ID, end_vertex_ID, start_exon, end_exon,
    start_delta, end_delta, read_length) to observed table of database."""

    abundance = {}
    with open(observed_file, "r") as f:
        while True:
            batch = []
            for observed in islice(f, batch_size):
                observed = observed.strip().split("\t")

                # Start/end delta and frac_A values may be None
                if observed[9] == "None":
                    observed[9] = None
                if observed[10] == "None":
                    observed[10] = None
                if observed[12] == "None":
                    observed[12] = None
                if observed[13] == "None":
                    observed[13] = None
                if observed[14] == "None":
                    observed[14] = None
                if observed[15] == "None":
                    observed[15] = None
                if observed[16] == "None":
                    observed[16] = None

                batch.append(tuple(observed))

                # Add record to abundance dict
                dataset = observed[4]
                transcript_ID = observed[2]

                if dataset not in abundance:
                    abundance[dataset] = {}

                try:
                    abundance[dataset][transcript_ID] += 1
                except:
                    abundance[dataset][transcript_ID] = 1

            if batch == []:
                break

            # Add to database
            try:
                cols = (
                    " ("
                    + ", ".join(
                        [
                            str_wrap_double(x)
                            for x in [
                                "obs_ID",
                                "gene_ID",
                                "transcript_ID",
                                "read_name",
                                "dataset",
                                "start_vertex",
                                "end_vertex",
                                "start_exon",
                                "end_exon",
                                "start_delta",
                                "end_delta",
                                "read_length",
                                "fraction_As",
                                "custom_label",
                                "allelic_label",
                                "start_support",
                                "end_support",
                            ]
                        ]
                    )
                    + ") "
                )
                command = 'INSERT INTO "observed"' + cols + "VALUES " + "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                cursor.executemany(command, batch)

            except Exception as e:
                logging.error(e)
                sys.exit(1)

    # Now create abundance tuples and add to DB
    abundance_tuples = []
    for dataset in abundance.keys():
        for transcript, count in abundance[dataset].items():
            abundance_tuples.append((transcript, dataset, count))

    batch_add_abundance(cursor, abundance_tuples, batch_size)
    return


def batch_add_abundance(cursor, entries, batch_size):
    """Reads abundance tuples (transcript_ID, dataset, count) and
    adds to the abundance table of the database"""

    index = 0
    while index < len(entries):
        try:
            batch = entries[index : index + batch_size]
        except:
            batch = entries[index:]
        index += batch_size

        try:
            cols = " (" + ", ".join([str_wrap_double(x) for x in ["transcript_id", "dataset", "count"]]) + ") "
            command = 'INSERT INTO "abundance"' + cols + "VALUES " + "(?,?,?)"
            cursor.executemany(command, batch)
        except Exception as e:
            logging.error(e)
            sys.exit(1)
    return


def check_database_integrity(cursor):
    """Perform some checks on the database. Run before committing changes"""

    # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    # print("[ %s ] Validating database........" % (ts))
    logging.info("Validating database")

    # For each category, check that the number of table entries matches the counter
    counter_query = "SELECT * FROM counters"
    cursor.execute(counter_query)
    counters = cursor.fetchall()
    fail = 0

    for table_name, curr_counter in counters:
        curr_counter = int(curr_counter)

        # Vertex case needs to be handled differently
        if table_name == "vertex":
            table_name = "location"

        query = "select COUNT(*) from " + table_name
        cursor.execute(query)
        actual_count = int(cursor.fetchone()[0])

        if actual_count != curr_counter:
            fail = 1
            logging.error(
                "Database counter for '"
                + table_name
                + "' does not match the number of entries in the table."
                + " Discarding changes to database and exiting..."
            )
            logging.debug("table_count: " + str(actual_count))
            logging.debug("counter_value: " + str(curr_counter))

    if fail == 1:
        msg = "Discrepancy found in database. " + "Discarding changes to database and exiting..."
        logging.error(msg)
        raise RuntimeError(msg)

    return


def parallel_talon(read_file, interval, database, run_info, queue):
    """Manage TALON processing of a single chunk of the input. Initialize
    reference data structures covering only the provided interval region,
    then send the read file to the annotation step. Once annotation is
    complete, return the data tuples generated so that they can be
    added to the database, OR alternately, pickle them and write to file
    where they can be accessed later."""

    # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    # print("[ %s ] Annotating reads in interval %s:%d-%d..." %
    #       (ts, interval[0], interval[1], interval[2]))
    logging.info(f"Annotating reads in interval {interval[0]}:{interval[1]}-{interval[2]}...")

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        tmp_id = str(os.getpid())
        struct_collection = prepare_data_structures(
            cursor, run_info, chrom=interval[0], start=interval[1], end=interval[2], tmp_id=tmp_id
        )

        interval_id = "%s_%d_%d" % interval

        with pysam.AlignmentFile(read_file, "rb") as sam:
            for record in sam:  # type: pysam.AlignedSegment
                # Check whether we should try annotating this read or not
                qc_metrics = tutils.check_read_quality(record, run_info)

                passed_qc = qc_metrics[2]
                qc_msg = (run_info.outfiles.qc, "\t".join([str(x) for x in qc_metrics]))
                queue.put(qc_msg)

                if passed_qc:
                    annotation_info = annotate_read(record, cursor, run_info, struct_collection)
                    unpack_observed(annotation_info, queue, run_info.outfiles.observed)

                    # Update annotation records
                    # TODO: there is no need for entry to be a list/tuple
                    for entry in annotation_info.gene_novelty:
                        msg = (run_info.outfiles.gene_annot, "\t".join([str(x) for x in entry]))
                        queue.put(msg)
                    for entry in annotation_info.transcript_novelty:
                        msg = (run_info.outfiles.transcript_annot, "\t".join([str(x) for x in entry]))
                        queue.put(msg)
                    for entry in annotation_info.exon_novelty:
                        msg = (run_info.outfiles.exon_annot, "\t".join([str(x) for x in entry]))
                        queue.put(msg)

        # Write the temp_gene table to file
        cursor.execute("SELECT gene_ID, strand FROM " + struct_collection.tmp_gene)
        for row in cursor.fetchall():
            msg = (run_info.outfiles.genes, str(row["gene_ID"]) + "\t" + row["strand"])
            queue.put(msg)

    # Pass messages to output files
    # ========================================================================
    # Write new transcripts to file
    transcripts = struct_collection.transcript_dict
    for transcript in list(transcripts.values()):
        # Only write novel transcripts to file
        if type(transcript) is dict:
            entry = "\t".join(
                [
                    str(x)
                    for x in (
                        transcript["transcript_ID"],
                        transcript["gene_ID"],
                        transcript["start_exon"],
                        transcript["jn_path"],
                        transcript["end_exon"],
                        transcript["start_vertex"],
                        transcript["end_vertex"],
                        transcript["n_exons"],
                    )
                ]
            )
            queue.put((run_info.outfiles.transcripts, entry))

    # Write new edges to file
    edges = struct_collection.edge_dict
    for edge in list(edges.values()):
        if type(edge) is dict:
            entry = "\t".join(
                [str(x) for x in [edge["edge_ID"], edge["v1"], edge["v2"], edge["edge_type"], edge["strand"]]]
            )
            queue.put((run_info.outfiles.edges, entry))

    # Write locations to file
    location_dict = struct_collection.location_dict
    for chrom_dict in location_dict.values():
        for loc in list(chrom_dict.values()):
            if type(loc) is dict:
                msg = (
                    run_info.outfiles.location,
                    "\t".join(
                        [str(x) for x in (loc["location_ID"], loc["genome_build"], loc["chromosome"], loc["position"])]
                    ),
                )
                queue.put(msg)

    # Write new vertex-gene combos to file
    for vertex_ID, gene_set in struct_collection.vertex_2_gene.items():
        for gene in gene_set:
            msg = (run_info.outfiles.v2g, "\t".join([str(x) for x in (vertex_ID, gene[0])]))
            queue.put(msg)

    struct_collection = None

    return


def parse_custom_SAM_tags(sam_record: pysam.AlignedSegment):
    """Looks for the following tags in the read. Will be set to None if no tag
    is found
        fA: fraction As in the 10-bp interval following the alignment end
        lC: custom label (type = string)
        lA: custom allele label (type = string)
        tS: flag indicating start site support (type = string)
        tE: flag indicating end site support (typ = string)
    """
    try:
        fraction_As = sam_record.get_tag("fA")
    except:
        fraction_As = None
    try:
        custom_label = sam_record.get_tag("lC")
    except:
        custom_label = None
    try:
        allelic_label = sam_record.get_tag("lA")
    except:
        allelic_label = None
    try:
        start_support = sam_record.get_tag("tS")
    except:
        start_support = None
    try:
        end_support = sam_record.get_tag("tE")
    except:
        end_support = None

    return fraction_As, custom_label, allelic_label, start_support, end_support


def annotate_read(sam_record: pysam.AlignedSegment, cursor, run_info, struct_collection, mode=1):
    """Accepts a pysam-formatted read as input, and compares it to the
    annotations in struct_collection to assign it a gene and transcript
    identity. Returns annotation_info, which is a dict that has the
    following attributes:
        gene_ID
        transcript_ID
        gene_novelty
        transcript_novelty
        exon_novelty
        start_vertex
        end_vertex
        start_exon
        end_exon
        start_delta
        end_delta
        fraction_As (following the end of the alignment)
        custom_label
        allelic_label
        start_support
        end_support
    """
    # Parse attributes to determine the chromosome, positions, and strand of the transcript
    read_ID = sam_record.query_name
    logging.debug('')
    logging.debug('')
    logging.debug(read_ID)
    if not run_info.use_cb_tag:
        dataset = sam_record.get_tag("RG")
    else:
        dataset = sam_record.get_tag("CB")

    chrom = sam_record.reference_name
    strand = "-" if sam_record.is_reverse else "+"
    sam_start = sam_record.reference_start + mode
    sam_end = sam_record.reference_end
    read_length = sam_record.query_alignment_length
    cigar = sam_record.cigarstring

    # Parse custom TALON tags
    fraction_As, custom_label, allelic_label, start_support, end_support = parse_custom_SAM_tags(sam_record)

    intron_list = tutils.get_introns(sam_record, sam_start, cigar)

    # Adjust intron positions by 1 to get splice sites in exon terms
    splice_sites = [x + 1 if i % 2 == 1 else x - 1 for i, x in enumerate(intron_list)]
    positions = [sam_start] + splice_sites + [sam_end]

    # Flip the positions' order if the read is on the minus strand
    if strand == "-":
        positions = positions[::-1]

    # Now identify the transcript
    location_dict = struct_collection.location_dict
    edge_dict = struct_collection.edge_dict
    transcript_dict = struct_collection.transcript_dict
    vertex_2_gene = struct_collection.vertex_2_gene
    gene_starts = struct_collection.gene_starts
    gene_ends = struct_collection.gene_ends

    n_exons = int(len(positions) / 2)
    if n_exons > 1:
        annotation_info = identify_transcript(
            chrom,
            positions,
            strand,
            cursor,
            location_dict,
            edge_dict,
            transcript_dict,
            vertex_2_gene,
            gene_starts,
            gene_ends,
            run_info,
            struct_collection.tmp_gene,
            struct_collection.tmp_t,
        )
    else:
        annotation_info = identify_monoexon_transcript(
            chrom,
            positions,
            strand,
            cursor,
            location_dict,
            edge_dict,
            transcript_dict,
            vertex_2_gene,
            gene_starts,
            gene_ends,
            run_info,
            struct_collection.tmp_gene,
            struct_collection.tmp_t,
            struct_collection.tmp_monoexon,
        )

    annotation_info.read_ID = read_ID
    annotation_info.dataset = dataset
    annotation_info.location = "%s:%d-%d" % (chrom, sam_start, sam_end)
    annotation_info.strand = strand
    annotation_info.read_length = read_length
    annotation_info.n_exons = n_exons
    annotation_info.fraction_As = fraction_As
    annotation_info.custom_label = custom_label
    annotation_info.allelic_label = allelic_label
    annotation_info.start_support = start_support
    annotation_info.end_support = end_support

    return annotation_info


def unpack_observed(annotation_info, queue, obs_file):
    """Now that transcript has been annotated, unpack values and
    create an observed entry. Send the observed entry to the queue
    for output to obs_file."""

    obs_ID = observed_counter.increment()
    observed = (
        obs_ID,
        annotation_info.gene_ID,
        annotation_info.transcript_ID,
        annotation_info.read_ID,
        annotation_info.dataset,
        annotation_info.start_vertex,
        annotation_info.end_vertex,
        annotation_info.start_exon,
        annotation_info.end_exon,
        annotation_info.start_delta,
        annotation_info.end_delta,
        annotation_info.read_length,
        annotation_info.fraction_As,
        annotation_info.custom_label,
        annotation_info.allelic_label,
        annotation_info.start_support,
        annotation_info.end_support,
    )
    msg = (obs_file, "\t".join([str(x) for x in observed]))
    queue.put(msg)

    return


def listener(queue, outfiles, QC_header, timeout=72):
    """During the run, this function listens for messages on the provided
    queue. When a message is received (consisting of a filename and a
    string), it writes the string to that file. Timeout unit is in hours"""

    # Open all of the outfiles
    open_files = {}
    for fpath in outfiles.values():
        open_files[fpath] = open(fpath, "w")

    # Add a header to the QC file
    QC_file = open_files[outfiles.qc]
    QC_file.write(QC_header + "\n")

    # Set a timeout
    wait_until = datetime.now() + timedelta(hours=timeout)

    while True:
        msg = queue.get()
        msg_fname = msg[0]
        msg_value = msg[1]
        if datetime.now() > wait_until or msg_value == "complete":
            # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            # print("[ %s ] Shutting down message queue..." % (ts))
            logging.info("Shutting down message queue...")
            for f in open_files.values():
                f.close()
            break

        open_files[msg_fname].write(msg_value + "\n")
        open_files[msg_fname].flush()


def make_QC_header(coverage, identity, length):
    """Create a header for the read QC file"""

    cols = "\t".join(
        ["dataset", "read_ID", "passed_QC", "primary_mapped", "read_length", "fraction_aligned", "identity"]
    )
    header = "\n".join(
        [
            "# TALON run filtering settings:",
            "# Min fraction read aligned: %f " % coverage,
            "# Min read identity to reference: %f" % identity,
            "# Min transcript length: %d" % length,
            "# -------------------------------------------",
            cols,
        ]
    )

    return header


def main():
    """Runs program"""
    options = get_args()
    logger._init_logger(options.verbosity)

    # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    # print("[ %s ] Started TALON run" % (ts))
    logging.info("Started TALON run")

    sam_files, dset_metadata = check_inputs(options)
    # print(sam_files)
    # print(dset_metadata[:5])
    # return

    threads = int(options.threads)
    if threads < 2:
        threads = 2  # Value of 1 will not work

    # Input parameters
    database = options.database
    build = options.build
    use_cb_tag = options.use_cb_tag
    min_coverage = float(options.min_coverage)
    min_identity = float(options.min_identity)
    outprefix = options.outprefix
    tmp_dir = options.tmp_dir
    create_novel_spliced_genes = bool(options.create_novel_spliced_genes)

    # format tmp_dir if missing fwd slash
    if not tmp_dir.endswith("/"):
        tmp_dir += "/"

    # Set globally accessible counters
    get_counters(database)

    # Initialize worker pool
    with mp.Pool(processes=threads) as pool:
        run_info = init_run_info(
            database, build, min_coverage, min_identity, use_cb_tag, create_novel_spliced_genes, tmp_dir=tmp_dir
        )
        run_info.outfiles = init_outfiles(options.outprefix, tmp_dir=tmp_dir)

        # Create annotation entry for each dataset
        datasets = []
        dataset_db_entries = []
        for d_name, description, platform in dset_metadata:
            d_id = dataset_counter.increment()
            datasets.append(d_name)
            dataset_db_entries.append((d_id, d_name, description, platform))

        # Partition the reads
        read_groups, intervals, header_file = procsams.partition_reads(
            sam_files, datasets, use_cb_tag, tmp_dir=tmp_dir, n_threads=threads
        )

        read_files = procsams.write_reads_to_file(read_groups, intervals, header_file, tmp_dir=tmp_dir)
        # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        logging.info(f"Split reads into {len(read_groups)} intervals")

        # Set up a queue specifically for writing to outfiles
        manager = mp.Manager()
        queue = manager.Queue()

        # Create job tuples to submit
        jobs = []
        for read_file, interval in zip(read_files, intervals):
            jobs.append((read_file, interval, database, run_info, queue))

        # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        # print("[ %s ] Launching parallel annotation jobs" % (ts))
        logging.info("Launching parallel annotation jobs")

        # Start running listener, which will monitor queue for messages
        QC_header = make_QC_header(run_info.min_coverage, run_info.min_identity, run_info.min_length)
        pool.apply_async(listener, (queue, run_info.outfiles, QC_header))

        # Now launch the parallel TALON jobs
        pool.starmap(parallel_talon, jobs)

        # Now we are done, kill the listener
        msg_done = (None, "complete")
        queue.put(msg_done)
        pool.close()
        pool.join()

    # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    # print("[ %s ] All jobs complete. Starting database update." % (ts))
    logging.info("All jobs complete. Starting database update")

    # Update the database
    batch_size = 10000
    update_database(database, batch_size, run_info.outfiles, dataset_db_entries)
    # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    # print("[ %s ] Database update complete." % (ts))
    logging.info("Database update complete.")

    # Write output reads file
    # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    # print("[ %s ] Creating read-wise annotation file." % (ts))
    logging.info("Creating read-wise annotation file")
    get_read_annotations.make_read_annot_file(database, build, outprefix, datasets=datasets)

    # For debugging
    # print("Genes: %d" % gene_counter.value())
    # print("Transcripts: %d" % transcript_counter.value())
    # print("Observed: %d" % observed_counter.value())

    # ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    # print("[ %s ] DONE" % (ts))
    logging.info("DONE")


if __name__ == "__main__":
    main()
