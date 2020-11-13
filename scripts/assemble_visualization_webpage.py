import io
import os
import re
import sys
import gzip
import json
import yaml
import codecs
import argparse
import urllib.parse
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib as mpl
import matplotlib.pyplot as plt

import vcf
import augur
from BCBio import GFF
from Bio import SeqIO
from Bio.Seq import Seq


def convert_vcf(fname):
    """Convert VCF to JSON."""
    output = []

    if os.path.getsize(fname) == 0:
        print(f'Empty VCF: "{fname}"')
        return output

    print(f'Parsing VCF: "{fname}"')
    with open(fname) as fd:
        vcf_reader = vcf.Reader(fd)

        for record in vcf_reader:
            output.append(
                {
                    "position": record.POS,
                    "reference": record.REF,
                    "variant": [v.sequence for v in record.ALT],
                    "frequency": round(np.mean(
                        [v for k, v in record.INFO.items() if k.startswith("Freq")]
                    ), 3),
                    "posterior": round(1 - 10**(-record.QUAL / 10), 3)
                }
            )

    return output


def get_metainfo(metainfo_yaml):
    """load metainformation for the GFF and primers from YAML file"""

    if metainfo_yaml:
        print(f'Parsing metainformation: "{metainfo_yaml}"')
        with open(metainfo_yaml) as fd:
            metainfo = yaml.load(fd.read(), Loader=yaml.SafeLoader)
        assert type(
            metainfo) is dict, f'Probable syntax error in {metainfo_yaml} - need a dictionnary at top level, got {type(metainfo)} instead.'
        return metainfo
    else:
        print("No metainformation YAML provided, skipping.")
        return {}


def assemble_variant_entry(
    vcf_entry,
    feature_start, feature_end,
    subfeature_list,
    consensus, consensus_variants
):
    """Warning: very naive and probably wrong."""
    variant_n_pos = vcf_entry.position - 1
    assert feature_start <= variant_n_pos <= feature_end

    aa_ref = '?'
    aa_var = '?'
    for subfeature in subfeature_list:
        if subfeature.type != 'CDS':
            continue

        start_pos = int(subfeature.location.start)
        end_pos = int(subfeature.location.end)

        if not start_pos <= variant_n_pos <= end_pos:
            continue

        aa_consensus = augur.translate.safe_translate(consensus[start_pos:end_pos])
        aa_consensus_variants = augur.translate.safe_translate(consensus_variants[start_pos:end_pos])

        variant_aa_subrelative_pos = (variant_n_pos - start_pos) // 3

        assert aa_ref == '?'  # assert we only have one overlapping CDS

        aa_ref = aa_consensus[variant_aa_subrelative_pos]
        aa_var = aa_consensus_variants[variant_aa_subrelative_pos]

    variant_aa_relative_pos = (variant_n_pos - feature_start) // 3

    # numbering start at 1 might be more natural to others
    true_pos = variant_aa_relative_pos + 1

    return (true_pos, true_pos, f'{aa_ref}{true_pos}{aa_var}')


def parse_gff(fname, df_vcf, consensus):
    """Convert GFF to map."""
    features = []

    # compute modified consensus
    # TODO: handle multiple variants in same position
    tmp = list(consensus)
    for row in df_vcf.itertuples():
        # assert tmp[row.position] == row.reference
        assert len(row.variant) == 1

        tmp[row.position] = row.variant[0]
    consensus_variants = ''.join(tmp)

    # parse GFF
    print(f'Parsing GFF: "{fname}"')
    with open(fname) as fd:
        for record in GFF.parse(fd):
            for feature in record.features:
                # extract protein range
                start_pos = int(feature.location.start)
                end_pos = int(feature.location.end)

                # assemble annotations
                df_sub = df_vcf[
                    (df_vcf['position'] >= start_pos) &
                    (df_vcf['position'] <= end_pos)
                ]

                protein_name = None
                if len(feature.sub_features) > 0:
                    protein_name = feature.sub_features[0].qualifiers.get('UNIPROT', [None])[0]

                swiss_model_url = ''
                if protein_name is not None:
                    variant_list = [assemble_variant_entry(
                                        entry,
                                        start_pos, end_pos,
                                        feature.sub_features,
                                        str(consensus), consensus_variants
                                    )
                                    for entry in df_sub.itertuples()]

                    swiss_model_url = generate_swissmodel_link(
                        protein_name, variant_list
                    )

                # finalize
                features.append(
                    {
                        "id": record.id,
                        "type": feature.type,
                        "name": feature.qualifiers.get('Name', [feature.id])[0],
                        "start": start_pos,
                        "end": end_pos,
                        "annotations": {
                            "protein_name": protein_name if protein_name is not None else 'undef',
                            "swiss-model": swiss_model_url
                        }
                    }
                )
    return features


def arrange_gff_data(features):
    """Add row number to each feature."""
    features.sort(key=lambda f: f["start"])

    rows = []
    for feature in features:
        if not rows:
            feature["row_cnt"] = 0
            rows.append([feature])
        else:
            found = False
            for idx, row in enumerate(rows):
                if row[-1]["end"] <= feature["start"]:
                    feature["row_cnt"] = idx
                    row.append(feature)
                    found = True
                    break
            if not found:
                feature["row_cnt"] = len(rows)
                rows.append([feature])

    return [item for row in rows for item in row]


def get_gff_data(gff_dir, df_vcf, consensus, gff_metainfo={}):
    """Returns a map with filename key and gff json data."""
    if gff_metainfo is None:
        gff_metainfo = {}
    assert type(
        gff_metainfo) is dict, f'Probable syntax error in metainfo YAML - need a dictionnary at [gff], got {type(gff_metainfo)} instead.'

    gff_map = {}
    if not gff_dir:
        print("No gff directory provided, skipping.")
        return gff_map
    for path in os.listdir(gff_dir):
        full_path = os.path.join(gff_dir, path)
        description = os.path.splitext(path)[0]
        if description in gff_metainfo:
            description = gff_metainfo[description]
        gff_map[description] = arrange_gff_data(parse_gff(full_path, df_vcf, consensus))
    return gff_map


def get_primers_data(full_path, consensus, primers_metainfo={}):
    """Returns a map with filename key and primers json data."""
    if primers_metainfo is None:
        primers_metainfo = {}
    assert type(
        primers_metainfo) is dict, f'Probable syntax error in metainfo YAML - need a dictionnary at [primers], got {type(primers_metainfo)} instead.'

    primers_map = {}
    if not full_path:
        print("No primers table provided, skipping.")
        return primers_map
    else:
        print(f'Parsing GFF: "{full_path}"')
    consensus_upper = consensus.upper()
    description = os.path.splitext(os.path.basename(full_path))[0]
    if description in primers_metainfo:
        description = primers_metainfo[description]
    csv = pd.read_csv(full_path, sep=',')
    primers = [{'name': row[0], "seq":row[1].upper()}
               for row in csv[['name', 'seq']].values]
    primer_locations = []
    for entry in primers:
        lookup_sequence = entry['seq']
        # If the sequence corresponds to a `right` primer, then reverse and
        # complement.
        if "RIGHT" in entry['name'].upper():
            seq = Seq(lookup_sequence)
            lookup_sequence = str(seq.reverse_complement())
        offsets = [
            m.start() for m in re.finditer(
                '(?=' + lookup_sequence + ')',
                consensus_upper)]
        for offset in offsets:
            primer_locations.append({'name': entry['name'],
                                     'seq': entry['seq'],
                                     'start': offset,
                                     'end': offset + len(entry['seq']) - 1})
    if primer_locations:
        primers_map[description] = arrange_gff_data(primer_locations)
    else:
        print(f'No primer was mapped from "{full_path}".')
    return primers_map


def convert_coverage(fname, sample_name=None):
    """Convert the read coverage to bp coverage."""
    print(f'Parsing coverage: "{fname}"')
    csv = pd.read_csv(fname, sep="\t",compression='infer',
                      index_col=['ref','pos'], header=0)
    col = None
    if len(csv.columns) == 1:
        if sample_name is not None and sample_name != csv.columns[0]:
            print(f'Ooops: column name "{csv.columns[0]}" in file is different from requested sample name "{sample_name}"!')
        col=csv[csv.columns[0]]
    else:
        assert sample_name is not None, 'Sample name is required when using combined coverage TSV.'
        col=csv[sample_name]
    return col.values.tolist()


def generate_swissmodel_link(protein_name, variant_list, colormap_name='rainbow'):
    """Generate link to SWISS-MODEL visualization.

    For now, we implement this in Python instead of Javascript,
    because the required data transformations (gzip, base64-encode, url-encode)
    are more asily implemented that way.

    Docs:
        * https://swissmodel.expasy.org/docs/repository_help#smr_parameters
        * https://swissmodel.expasy.org/repository/user_annotation_upload
        * https://matplotlib.org/3.3.2/tutorials/colors/colormaps.html
    """
    # convert variant list to string representation
    reference = 'https://github.com/cbg-ethz/V-pipe'
    color_map = plt.cm.get_cmap(colormap_name, len(variant_list))

    string_content = ''
    for i, entry in enumerate(variant_list):
        start_pos, end_pos, annotation = entry
        color = mpl.colors.to_hex(color_map(i))

        string_content += f'{protein_name} {start_pos} {end_pos} {color} {reference} {annotation}\n'

    # transform string to required format
    bytes_content = string_content.encode()

    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode='w', compresslevel=9) as fd:
        fd.write(bytes_content)
    zlib_content = out.getvalue()

    base64_content = codecs.encode(zlib_content, 'base64')
    urlquoted_content = urllib.parse.quote(base64_content.rstrip(b'\n'))

    # embed in URL
    base_url = 'https://swissmodel.expasy.org/repository/uniprot/{protein_name}?annot={query}'

    return base_url.format(protein_name=protein_name, query=urlquoted_content)


def assemble_visualization_webpage(
    consensus_file,
    coverage_file,
    vcf_file,
    gff_directory,
    primers_file,
    html_file_in,
    html_file_out,
    wildcards_dataset,
    reference_file,
    metainfo_yaml,
):
    # parse the sample name
    path_components = os.path.normpath(wildcards_dataset).split(os.path.sep)
    sample_name = '/'.join(path_components[-2:])

    # parse the consensus sequence
    print(f'Parsing consensus: "{consensus_file}"')
    consensus = next(SeqIO.parse(consensus_file, "fasta")).seq.upper()

    # parse coverage file
    coverage = convert_coverage(
        coverage_file,
        sample_name.replace('/', '-'))

    # load biodata in json format
    vcf_data = convert_vcf(vcf_file)
    metainfo = get_metainfo(metainfo_yaml)
    gff_map = get_gff_data(gff_directory,
                           pd.DataFrame(vcf_data), consensus,
                           gff_metainfo=metainfo['gff'] if 'gff' in metainfo else {})
    primers_map = get_primers_data(primers_file, str(consensus),
                                   primers_metainfo=metainfo['primers'] if 'primers' in metainfo else {})

    vcf_json = json.dumps(vcf_data)

    # parse the reference name
    reference_name = re.search(
        r"(.+)\.fa.*",
        os.path.basename(reference_file)
    ).group(1).replace('_', '&nbsp;')

    embed_code = f"""
        var sample_name = \"{sample_name}\"
        var consensus = \"{consensus}\"
        var coverage = {coverage}
        var vcfData = {vcf_json}
        var gffData = {gff_map}
        var primerData = {primers_map}
        var reference_name = \"{reference_name}\"
    """

    # assemble webpage
    with open(html_file_in) as fd:
        raw_html = fd.read()

    # TODO: make this more robust
    mod_html = raw_html.replace("{EXTERNAL_SNAKEMAKE_CODE_MARKER}", embed_code)

    with open(html_file_out, "w") as fd:
        fd.write(mod_html)


def main():
    """Parse command line, run default functions."""
    # parse command line
    parser = argparse.ArgumentParser(description="Generate HTML visual report from VCF variants",
                                     epilog="at minimum, either provide `-v` and `-c` or provide `-w`")
    parser.add_argument('-f', '--consensus', metavar='FASTA', required=False,
                        type=str, dest='consensus_file', help="consensus sequence of this sample")
    parser.add_argument('-c', '--coverage', metavar='TSV', required=False,
                        default='variants/coverage.tsv',
                        type=str, dest='coverage_file', help="global coverage table")
    parser.add_argument('-v', '--vcf', metavar='VCF', required=False,
                        type=str, dest='vcf_file', help="VCF containing the SNPs to be visualised")
    parser.add_argument('-g', '--gff', metavar='DIR', required=False,
                        type=str, dest='gff_directory', help="directory containing GFF annotations")
    parser.add_argument('-p', '--primers', metavar='CSV', required=False,
                        type=str, dest='primers_file', help="table with primers")
    parser.add_argument('-m', '--metainfo', metavar='YAML', required=False,
                        type=str, dest='metainfo_yaml', help="metainformation for the GFF and primers")
    parser.add_argument('-t', '--template', metavar='HTML', required=False,
                        default=f'{os.path.dirname(__file__)}/visualization.html',
                        type=str, dest='html_file_in', help="HTML template used to generate visual report")
    parser.add_argument('-o', '--output', metavar='HTML', required=False,
                        type=str, dest='html_file_out', help="produced HTML report")
    parser.add_argument('-w', '--wildcards', metavar='SAMPLE/DATE', required=False,
                        type=str, dest='wildcards_dataset', help="sample's two-level directory hierarchy prefix")
    parser.add_argument('-r', '--reference', metavar='FASTA', required=False,
                        default='variants/cohort_consensus.fasta',
                        type=str, dest='reference_file', help="reference against which SNVs were called (e.g.: cohort's consensus)")

    args = parser.parse_args()

    # defaults which can be guess from one another
    if args.vcf_file is None:  # e.g.: samples/140074_395_D02/20200615_J6NRK/variants/SNVs/snvs.vcf
        assert args.wildcards_dataset is not None, 'cannot automatically find VCF without wildcards'
        args.vcf_file = os.path.join(
            args.wildcards_dataset, 'variants', 'SNVs', 'snvs.vcf')

    if args.consensus_file is None:  # e.g.: samples/140074_395_D02/20200615_J6NRK/references/ref_majority.fasta
        assert args.wildcards_dataset is not None, 'cannot automatically find consensus without wildcards'
        args.consensus_file = os.path.join(
            args.wildcards_dataset, 'references', 'ref_majority.fasta')

    if args.wildcards_dataset is None:
        assert args.vcf_file is not None and args.consensus is not None, 'cannot deduce wilcards without a consensus and a vcf'
        try1 = '/'.join(os.path.normpath(args.vcf_file)
                        .split(os.path.sep)[-5:-3])
        try2 = '/'.join(os.path.normpath(args.consensus_file)
                        .split(os.path.sep)[-4:-2])
        assert try1 == try2, f'cannot deduce wildcards automatically from <{args.vcf_file}> and <{args.consensus_file}>, please specify explicitly using `--wirdcards`'
        args.wildcards_dataset = try1

    if args.html_file_out is None:
        args.html_file_out = os.path.join(
            args.wildcards_dataset, 'visualization', 'index.html')

    # check mandatory files exist
    for n, f in {'vcf': args.vcf_file,
                 'consensus': args.consensus_file,
                 'coverage': args.coverage_file,
                 'template': args.html_file_in,
                 }.items():
        if not os.path.exists(f):
            parser.error(f"{n} file <{f}> does not exist!")

    # check optional files exist if specified
    for n, f in {'gff': args.gff_directory,
                 'primers': args.primers_file,
                 'metainfo': args.metainfo_yaml,
                 }.items():
        if f and not os.path.exists(f):
            parser.error(f"{n} file <{f}> does not exist!")

    # run the visual report generator
    assemble_visualization_webpage(**vars(args))


if __name__ == "__main__":
    main()
