#!/usr/bin/env python3
"""Amend genotype field to contain all variants."""

import sys

from cyvcf2 import VCF, Writer


def main(fname_in, fname_out, ambiguous_base_coverage_threshold):
    """
    ambiguous_base_coverage_threshold:
        frequency threshold to include a variant in computation of ambiguous code
    """
    vcf_reader = VCF(fname_in)
    vcf_writer = Writer(fname_out, vcf_reader)

    for variant in vcf_reader:
        base_list = [variant.REF] + variant.ALT
        coverage_list = variant.INFO.get('AD')

        total_coverage = sum(coverage_list)
        assert len(base_list) == len(coverage_list)

        # genotype 0 is reference (base is not really needed)
        genotype = [
            i
            for i, (base, coverage) in enumerate(zip(base_list, coverage_list))
            if coverage / total_coverage >= ambiguous_base_coverage_threshold
        ]

        variant.genotypes = [[*genotype, False]]

        vcf_writer.write_record(variant)

    vcf_writer.close()
    vcf_reader.close()


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]))
