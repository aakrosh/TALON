# TALON: Techonology-Agnostic Long Read Analysis Pipeline
# Author: Dana Wyman
# -----------------------------------------------------------------------------
# filter_talon_transcripts.py is a utility that filters the transcripts inside
# a TALON database to produce a transcript whitelist. This list can then be
# used by downstream analysis tools to determine which transcripts and other
# features should be reported (for example in a GTF file).

import os
import sqlite3
import warnings
from optparse import OptionParser
from pathlib import Path

import pandas as pd

from talon.post import get_read_annotations as read_annot

from .. import query_utils as qutils
from . import ab_utils as autils


def getOptions():
    parser = OptionParser(
        description=(
            "talon_filter_transcripts is a "
            "utility that filters the transcripts inside "
            "a TALON database to produce a transcript pass list. "
            "This list can then be used by downstream analysis "
            "tools to determine which transcripts and other "
            "features should be reported (for example in a GTF file)"
        )
    )
    parser.add_option("--db", dest="database", help="TALON database", metavar="FILE", type=str)
    parser.add_option(
        "--annot",
        "-a",
        dest="annot",
        help="""Which annotation version to use. Will determine which
                              annotation transcripts are considered known or novel
                              relative to. Note: must be in the TALON database.""",
        type="string",
    )
    parser.add_option(
        "--filter_known",
        dest="filter_known",
        help="""Filter known transcripts according to the same input criteria;
                              will not automatically pass them""",
        default=False,
        action="store_true"
    )
    parser.add_option(
        "--datasets",
        dest="datasets",
        default=None,
        help=(
            "Datasets to include. Can be provided as a "
            "comma-delimited list on the command line, "
            "or as a file with one dataset per line. "
            "If this option is omitted, all datasets will "
            "be included."
        ),
    )
    parser.add_option(
        "--includeAnnot",
        dest="include_annot",
        action="store_true",
        help=("Include all transcripts from the annotation, regardless " "of if they were observed in the data."),
    )
    parser.add_option(
        "--maxFracA",
        dest="max_frac_A",
        default=0.5,
        help=(
            "Maximum fraction of As to allow in the window "
            "located immediately after any read assigned to "
            "a novel transcript (helps to filter out internal "
            "priming artifacts). Default = 0.5. Use 1 if you prefer"
            "to not filter out internal priming events."
        ),
        type=float,
    )
    parser.add_option(
        "--minCount",
        dest="min_count",
        default=5,
        type=int,
        help=("Number of minimum occurrences required for a " "novel transcript PER dataset. Default = 5"),
    )
    parser.add_option(
        "--minDatasets",
        dest="min_datasets",
        default=None,
        type=int,
        help=("Minimum number of datasets novel transcripts " "must be found in. Default = all datasets provided"),
    )
    parser.add_option(
        "--allowGenomic",
        dest="allow_genomic",
        action="store_true",
        help=(
            "If this option is set, transcripts from the Genomic "
            "novelty category will be permitted in the output "
            "(provided they pass the thresholds). Default "
            "behavior is to filter out genomic transcripts "
            "since they are unlikely to be real novel isoforms."
        ),
        default=False,
    )
    parser.add_option(
        "--excludeISM",
        dest="exclude_ISMs",
        action="store_true",
        help=(
            "If this option is set, transcripts from the ISM "
            "novelty category will be excluded from the output. "
            "Default behavior is to include those that pass other "
            "filtering thresholds."
        ),
    )
    parser.add_option("--o", dest="outfile", help="Outfile name", metavar="FILE", type="string")

    (options, args) = parser.parse_args()
    return options


def get_known_transcripts(database, annot, include_annot, datasets=None):
    """Fetch gene ID and transcript ID of all known transcripts detected in
    the specified datasets"""

    with sqlite3.connect(database) as conn:
        # pull from observed table
        if not include_annot:
            query = """SELECT DISTINCT gene_ID, transcript_ID FROM observed
                           LEFT JOIN transcript_annotations AS ta
                               ON ta.ID = observed.transcript_ID
                           WHERE (ta.attribute = 'transcript_status'
                                  AND ta.value = 'KNOWN'
                                  AND ta.annot_name = '%s')""" % (
                annot
            )

        # pull from normal transcripts table
        elif include_annot:
            query = f"""SELECT DISTINCT t.gene_ID, t.transcript_ID
                FROM transcripts as t
                LEFT JOIN transcript_annotations as ta
                    ON ta.ID = t.transcript_ID
                WHERE (ta.attribute = 'transcript_status'
                    AND ta.value = 'KNOWN'
                    AND ta.annot_name = '{annot}')
                     """

        # limit to datasets that transcript is seen in if requested
        # if we requested to include all annotated transcripts, we don't need
        # to do this
        if datasets != None and not include_annot:
            datasets = qutils.format_for_IN(datasets)
            query += " AND observed.dataset IN " + datasets
        known = pd.read_sql_query(query, conn)

    return known


def fetch_reads_in_datasets_fracA_cutoff(database, datasets, max_frac_A):
    """Selects reads from the database that are from the specified datasets
    and which pass the following cutoffs:
        - fraction_As <= max_frac_A
    Reads with fraction_As value of None will not be included.
    If datasets == None, then all datasets are permitted"""

    # convert non-iterable datasets to an iterable
    if datasets == None:
        with sqlite3.connect(database) as conn:
            query = """SELECT dataset_name
                       FROM dataset"""
            iter_datasets = pd.read_sql_query(query, conn).dataset_name.tolist()
    else:
        iter_datasets = datasets

    # first check if we have non-null fraction_As columns at all
    # (one dataset at a time)
    for dataset in iter_datasets:
        with sqlite3.connect(database) as conn:
            query = """SELECT read_name, gene_ID, transcript_ID, dataset, fraction_As
                         FROM observed WHERE dataset='{}' LIMIT 0, 10""".format(
                dataset
            )

            data = pd.read_sql_query(query, conn)
            nans = all(data.fraction_As.isna().tolist())

            if nans and max_frac_A != 1:
                print(
                    "Reads in dataset {} appear to be unlabelled. "
                    "Only known transcripts will pass the filter.".format(dataset)
                )

    with sqlite3.connect(database) as conn:
        query = """SELECT read_name, gene_ID, transcript_ID, dataset, fraction_As
                       FROM observed
                       WHERE fraction_As <= %f""" % (
            max_frac_A
        )
        if datasets != None:
            datasets = qutils.format_for_IN(datasets)
            query += " AND dataset IN " + datasets

        data = pd.read_sql_query(query, conn)

    # warn the user if no novel models passed filtering
    if len(data.index) == 0:
        print("No reads passed maxFracA cutoff. Is this expected?")

    return data


# def check_annot_validity(annot, database):
#     """ Make sure that the user has entered a correct annotation name """
#
#     conn = sqlite3.connect(database)
#     cursor = conn.cursor()
#
#     cursor.execute("SELECT DISTINCT annot_name FROM gene_annotations")
#     annotations = [str(x[0]) for x in cursor.fetchall()]
#     conn.close()
#
#     if "TALON" in annotations:
#         annotations.remove("TALON")
#
#     if annot == None:
#         message = "Please provide a valid annotation name. " + \
#                   "In this database, your options are: " + \
#                   ", ".join(annotations)
#         raise ValueError(message)
#
#     if annot not in annotations:
#         message = "Annotation name '" + annot + \
#                   "' not found in this database. Try one of the following: " + \
#                   ", ".join(annotations)
#         raise ValueError(message)
#
#     return


def check_db_version(database):
    """Make sure the user is using a v5 database"""
    conn = sqlite3.connect(database)
    cursor = conn.cursor()

    with sqlite3.connect(database) as conn:
        query = """SELECT value
                       FROM run_info
                       WHERE item='schema_version'"""
        ver = pd.read_sql_query(query, conn)

        if ver.empty:
            message = "Database version is not compatible with v5.0 filtering."
            raise ValueError(message)


def parse_datasets(dataset_option, database):
    """Parses dataset names from command line. Valid forms of input:
        - None (returns None)
        - Comma-delimited list of names
        - File of names (One per line)
    Also checks to make sure that the datasets are in the database.
    """
    if dataset_option == None:
        print(("No dataset names specified, so filtering process will use all " "datasets present in the database."))
        return None

    elif os.path.isfile(dataset_option):
        print("Parsing datasets from file %s..." % (dataset_option))
        datasets = []
        with open(dataset_option) as f:
            for line in f:
                line = line.strip()
                datasets.append(line)
    else:
        datasets = dataset_option.split(",")

    # Now validate the datasets
    with sqlite3.connect(database) as conn:
        cursor = conn.cursor()
        valid_datasets = qutils.fetch_all_datasets(cursor)
        invalid_datasets = []
        for dset in datasets:
            if dset not in valid_datasets:
                invalid_datasets.append(dset)
        if len(invalid_datasets) > 0:
            raise ValueError(
                (
                    "Problem parsing datasets. The following names are "
                    "not in the database: '%s'. \nValid dataset names: '%s'"
                )
                % (", ".join(invalid_datasets), ", ".join(valid_datasets))
            )
        else:
            print("Parsed the following dataset names successfully: %s" % (", ".join(datasets)))
    return datasets


def get_novelty_df(database):
    """Get the novelty category assignment of each transcript and
    store in a data frame"""

    transcript_novelty_dict = read_annot.get_transcript_novelty(database)
    transcript_novelty = pd.DataFrame.from_dict(transcript_novelty_dict, orient="index")
    transcript_novelty = transcript_novelty.reset_index()
    transcript_novelty.columns = ["transcript_ID", "transcript_novelty"]

    return transcript_novelty


def merge_reads_with_novelty(reads, novelty):
    """Given a data frame of reads and a transcript novelty data frame,
    perform a left merge to annotate the reads with their novelty status.
    """

    merged = pd.merge(reads, novelty, on="transcript_ID", how="left")
    return merged


def filter_on_min_count(reads, min_count):
    """Given a reads data frame, compute the number of times that each
    transcript ID occurs per dataset.
    Keep the rows that meet the min_count threshold and return them."""

    cols = ["gene_ID", "transcript_ID", "dataset"]

    counts_df = reads[cols].groupby(cols).size()
    counts_df = counts_df.reset_index()
    counts_df.columns = cols + ["count"]

    filtered = counts_df.loc[counts_df["count"] >= min_count]
    return filtered


def filter_on_n_datasets(counts_in_datasets, min_datasets):
    """Given a data frame with columns gene_ID, transcript_ID, dataset,
    and count (in that dataset), count the number of datasets that each
    transcript appears in. Then, filter the data such that only transcripts
    found in at least 'min_datasets' remain."""

    cols = ["gene_ID", "transcript_ID"]
    dataset_count_df = counts_in_datasets[cols].groupby(cols).size()
    dataset_count_df = dataset_count_df.reset_index()
    dataset_count_df.columns = cols + ["n_datasets"]

    filtered = dataset_count_df.loc[dataset_count_df["n_datasets"] >= min_datasets]
    return filtered


def filter_talon_transcripts(database, annot, datasets, options):
    """Filter transcripts belonging to the specified datasets in a TALON
    database. The 'annot' parameter specifies which annotation transcripts
    are known relative to. Can be tuned with the following options:
    - options.include_annot: Include all annotated transcripts regardless
                             of whether they are expressed
    - options.max_frac_A: maximum allowable fraction of As recorded for
                          region after the read (0-1)
    - options.allow_genomic: Removes genomic transcripts if set to False
    - options.exlude_ISMs: Removes ISM transcripts if set to True
    - options.min_count: Transcripts must appear at least this many times
                         to count as present in a dataset
    - options.min_datasets: After the min_count threshold has been
                            applied, the transcript must be found in at
                            least this many datasets to pass the filter.
                            If this option is set to None, then it will
                            default to the total number of datasets in the
                            reads.
    - options.filter_known: Filter known transcripts the same way that novel
                            transcripts are filtered
    Please note that known transcripts are allowed through independently
    of these parameters, unless the filter_known option is on
    """
    # Known transcripts automatically pass the filter
    known = get_known_transcripts(database, annot, options.include_annot, datasets=datasets)

    # Get reads that pass fraction A cutoff
    reads = fetch_reads_in_datasets_fracA_cutoff(database, datasets, options.max_frac_A)

    # Fetch novelty information and merge with reads
    reads = merge_reads_with_novelty(reads, get_novelty_df(database))

    # Drop genomic transcripts if desired
    if options.allow_genomic == False:
        reads = reads.loc[reads.transcript_novelty != "Genomic"]

    # Drop ISMs if desired
    if options.exclude_ISMs == True:
        reads = reads.loc[reads.transcript_novelty != "ISM"]

    # Perform counts-based filtering
    filtered_counts = filter_on_min_count(reads, options.min_count)

    # Perform n-dataset based filtering
    if options.min_datasets == None:
        options.min_datasets = len(set(list(reads.dataset)))
    elif options.min_datasets > len(set(list(reads.dataset))):
        print(f'min_datasets value {options.min_datasets} is larger than total # of datasets {len(reads.dataset.unique())}.')
        print(f'Changing min_datasets to {len(reads.dataset.unique())}')
        options.min_datasets = len(reads.dataset.unique())
    dataset_filtered = filter_on_n_datasets(filtered_counts, options.min_datasets)

    # Join the known transcripts with the filtered ones and return
    if len(dataset_filtered.index) != 0 and not options.filter_known:
        final_filtered = pd.concat(
            [known[["gene_ID", "transcript_ID"]], dataset_filtered[["gene_ID", "transcript_ID"]]]
        ).drop_duplicates()
    elif options.filter_known:
        final_filtered = dataset_filtered[["gene_ID", "transcript_ID"]]
    else:
        final_filtered = known

    return final_filtered


def main():
    options = getOptions()
    database = options.database
    annot = options.annot

    # Make sure that the input database exists!
    if not Path(database).exists():
        raise ValueError("Database file '%s' does not exist!" % database)

    # Make sure the database is of the v5 schema
    check_db_version(database)

    # Make sure that the provided annotation name is valid
    autils.check_annot_validity(annot, database)

    # Parse datasets
    datasets = parse_datasets(options.datasets, database)
    if datasets != None and len(datasets) == 1:
        warnings.warn(
            "Only one dataset provided. For best performance, please "
            "run TALON with at least 2 biological replicates if possible."
        )

    # Perform the filtering
    filtered = filter_talon_transcripts(database, annot, datasets, options)

    # Write gene and transcript IDs to file
    print("Writing gene-transcript TALON ID pairs that passed filtering to " + options.outfile + "...")
    filtered.to_csv(options.outfile, sep=",", header=False, index=False)


if __name__ == "__main__":
    main()
