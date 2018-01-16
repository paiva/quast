############################################################################
# Copyright (c) 2015-2017 Saint Petersburg State University
# Copyright (c) 2011-2015 Saint Petersburg Academic University
# All Rights Reserved
# See file LICENSE for details.
############################################################################

from __future__ import with_statement

import os
import shutil
from collections import defaultdict
from os.path import join, abspath, exists, basename, isdir

from quast_libs import qconfig, reporting, qutils
from quast_libs.fastaparser import read_fasta
from quast_libs.qutils import get_free_memory, md5, download_external_tool, \
    get_dir_for_download
from quast_libs.ra_utils.misc import minimap_fpath, compile_minimap
from quast_libs.reporting import save_kmers

KMERS_LEN = 101
KMER_FRACTION = 0.001
KMERS_INTERVAL = 1000
MAX_CONTIGS_NUM = 10000
MAX_REF_CONTIGS_NUM = 200
MIN_CONTIGS_LEN = 10000
EXT_RELOCATION_SIZE = 100000

kmc_dirname = 'kmc'
kmc_dirpath = abspath(join(qconfig.LIBS_LOCATION, kmc_dirname, qconfig.platform_name))
kmc_bin_fpath = join(kmc_dirpath, 'kmc')
kmc_tools_fpath = join(kmc_dirpath, 'kmc_tools')


def create_kmc_stats_file(output_dir, contigs_fpath, ref_fpath, completeness,
                          corr_len, mis_len, undef_len, total_len, misassemblies):
    label = qutils.label_from_fpath_for_fname(contigs_fpath)
    kmc_check_fpath = join(output_dir, label + '.sf')
    kmc_stats_fpath = join(output_dir, label + '.stat')
    with open(kmc_check_fpath, 'w') as check_f:
        check_f.write("Assembly md5 checksum: %s\n" % md5(contigs_fpath))
        check_f.write("Reference md5 checksum: %s\n" % md5(ref_fpath))
    with open(kmc_stats_fpath, 'w') as stats_f:
        stats_f.write("Completeness: %s\n" % completeness)
        if corr_len or mis_len:
            stats_f.write("K-mer-based correct length: %d\n" % corr_len)
            stats_f.write("K-mer-based misjoined length: %d\n" % mis_len)
            stats_f.write("K-mer-based undefined length: %d\n" % undef_len)
            stats_f.write("Total length: %d\n" % total_len)
            stats_f.write("# misjoins: %d\n" % misassemblies)


def check_kmc_successful_check(output_dir, contigs_fpath, contigs_fpaths, ref_fpath):
    label = qutils.label_from_fpath_for_fname(contigs_fpath)
    kmc_check_fpath = join(output_dir, label + '.sf')
    if not exists(kmc_check_fpath):
        return False
    successful_check_content = open(kmc_check_fpath).read().split('\n')
    if len(successful_check_content) < 2:
        return False
    if successful_check_content[0].strip().split()[-1] != str(md5(contigs_fpath)):
        return False
    if successful_check_content[1].strip().split()[-1] != str(md5(ref_fpath)):
        return False
    return True


def get_kmers_cnt(tmp_dirpath, kmc_db_fpath, log_fpath, err_fpath):
    histo_fpath = join(tmp_dirpath, basename(kmc_db_fpath) + '.histo.txt')
    run_kmc(['histogram', kmc_db_fpath, histo_fpath], log_fpath, err_fpath)
    kmers_cnt = 0
    if exists(histo_fpath):
        kmers_cnt = int(open(histo_fpath).read().split()[-1])
    return kmers_cnt


def count_kmers(tmp_dirpath, fpath, log_fpath, err_fpath):
    kmc_out_fpath = join(tmp_dirpath, basename(fpath) + '.kmc')
    max_mem = max(2, get_free_memory())
    run_kmc(['-m' + str(max_mem), '-k' + str(KMERS_LEN), '-fm', '-cx1', '-ci1', fpath, kmc_out_fpath, tmp_dirpath],
            log_fpath, err_fpath, use_kmc_tools=False)
    return kmc_out_fpath


def align_kmers(output_dir, ref_fpath, kmers_fpath, log_err_fpath, max_threads):
    out_fpath = join(output_dir, 'kmers.coords')
    cmdline = [minimap_fpath(), '-ax', 'sr', '-s202', '-t', str(max_threads), ref_fpath, kmers_fpath]
    qutils.call_subprocess(cmdline, stdout=open(out_fpath, 'w'), stderr=open(log_err_fpath, 'a'), indent='  ')
    kmers_pos_by_chrom = defaultdict(list)
    kmers_by_chrom = defaultdict(list)
    with open(out_fpath) as f:
        for line in f:
            fs = line.split('\t')
            if len(fs) < 10:
                continue
            contig, chrom, pos = fs[0], fs[2], fs[3]
            kmers_pos_by_chrom[chrom].append(int(pos))
            kmers_by_chrom[chrom].append(int(contig))
    return kmers_by_chrom, kmers_pos_by_chrom


def downsample_kmers(tmp_dirpath, ref_fpath, kmc_out_fpath, log_fpath, err_fpath):
    kmc_txt_fpath = join(tmp_dirpath, 'kmc.full.txt')
    downsampled_txt_fpath = join(tmp_dirpath, 'kmc.downsampled.txt')
    run_kmc(['transform', kmc_out_fpath, 'dump', kmc_txt_fpath], log_fpath, err_fpath)
    kmc_fasta_fpath = join(tmp_dirpath, 'kmc.full.fasta')
    with open(kmc_txt_fpath) as in_f:
        with open(kmc_fasta_fpath, 'w') as out_f:
            for kmer_i, line in enumerate(in_f):
                kmer, _ = line.split()
                out_f.write('>' + str(kmer_i) + '\n')
                out_f.write(kmer + '\n')

    kmers_by_chrom, kmers_pos_by_chrom = align_kmers(tmp_dirpath, ref_fpath, kmc_fasta_fpath, err_fpath, qconfig.max_threads)
    ref_kmers = dict()
    for chrom in kmers_by_chrom.keys():
        sorted_kmers = [(kmer, kmer_pos) for kmer_pos, kmer in
                        sorted(zip(kmers_pos_by_chrom[chrom], kmers_by_chrom[chrom]), key=lambda x: x[0])]
        for kmer, kmer_pos in sorted_kmers[::KMERS_INTERVAL]:
            ref_kmers[kmer] = (chrom, kmer_pos)
    with open(kmc_txt_fpath) as in_f:
        with open(downsampled_txt_fpath, 'w') as out_f:
            for kmer_i, line in enumerate(in_f):
                if kmer_i in ref_kmers:
                    kmer, _ = line.split()
                    out_f.write('>' + str(kmer_i) + '\n')
                    out_f.write(kmer + '\n')
    return ref_kmers, downsampled_txt_fpath


def kmc_to_str(tmp_dirpath, kmc_out_fpath, log_fpath, err_fpath, kmer_fraction=1):
    kmers = set()
    kmc_txt_fpath = join(tmp_dirpath, 'tmp.kmc.txt')
    run_kmc(['transform', kmc_out_fpath, 'dump', kmc_txt_fpath], log_fpath, err_fpath)
    with open(kmc_txt_fpath) as kmer_in:
        for kmer_i, line in enumerate(kmer_in):
            if kmer_i % kmer_fraction == 0:
                kmer, _ = line.split()
                kmers.add(kmer)
    return kmers


def get_clear_name(fpath):
    return basename(fpath).replace('.kmc', '')


def intersect_kmers(tmp_dirpath, kmc_out_fpaths, log_fpath, err_fpath):
    intersect_out_fpath = join(tmp_dirpath, '_'.join([get_clear_name(kmc_out_fpath)[:30] for kmc_out_fpath in kmc_out_fpaths]) + '.kmc')
    if len(kmc_out_fpaths) == 2:
        run_kmc(['simple'] + kmc_out_fpaths + ['intersect', intersect_out_fpath], log_fpath, err_fpath)
    else:
        prev_kmc_out_fpath = kmc_out_fpaths[0]
        for i in range(1, len(kmc_out_fpaths)):
            tmp_out_fpath = join(tmp_dirpath, get_clear_name(prev_kmc_out_fpath) + '_' + str(i) + '.kmc')
            run_kmc(['simple', prev_kmc_out_fpath, kmc_out_fpaths[i], 'intersect', tmp_out_fpath], log_fpath, err_fpath)
            prev_kmc_out_fpath = tmp_out_fpath
        intersect_out_fpath = prev_kmc_out_fpath
    return intersect_out_fpath


def filter_contigs(input_fpath, output_fpath, db_fpath, log_fpath, err_fpath, min_kmers=1):
    if input_fpath.endswith('.txt'):
        input_fpath = '@' + input_fpath
    run_kmc(['filter', db_fpath, input_fpath, '-ci' + str(min_kmers), '-fa', output_fpath], log_fpath, err_fpath)


def run_kmc(params, log_fpath, err_fpath, use_kmc_tools=True):
    tool_fpath = kmc_tools_fpath if use_kmc_tools else kmc_bin_fpath
    qutils.call_subprocess([tool_fpath, '-t' + str(qconfig.max_threads), '-hp'] + params,
                           stdout=open(log_fpath, 'a'), stderr=open(err_fpath, 'a'))


def _get_dist_inconstistency(pos, prev_pos, ref_pos, prev_ref_pos):
    return abs(abs(pos - prev_pos) - abs(ref_pos - prev_ref_pos))


def do(output_dir, ref_fpath, contigs_fpaths, logger):
    logger.print_timestamp()
    logger.main_info('Running analysis based on unique ' + str(KMERS_LEN) + '-mers...')

    checked_assemblies = []
    for contigs_fpath in contigs_fpaths:
        label = qutils.label_from_fpath_for_fname(contigs_fpath)
        if check_kmc_successful_check(output_dir, contigs_fpath, contigs_fpaths, ref_fpath):
            kmc_stats_fpath = join(output_dir, label + '.stat')
            stats_content = open(kmc_stats_fpath).read().split('\n')
            if len(stats_content) < 1:
                continue
            logger.info('  Using existing results for ' + label + '... ')
            report = reporting.get(contigs_fpath)
            report.add_field(reporting.Fields.KMER_COMPLETENESS, '%.2f' % float(stats_content[0].strip().split(': ')[-1]))
            if len(stats_content) >= 6:
                corr_len = int(stats_content[1].strip().split(': ')[-1])
                mis_len = int(stats_content[2].strip().split(': ')[-1])
                undef_len = int(stats_content[3].strip().split(': ')[-1])
                total_len = int(stats_content[4].strip().split(': ')[-1])
                misassemblies = int(stats_content[5].strip().split(': ')[-1])
                report.add_field(reporting.Fields.KMER_CORR_LENGTH, '%.2f' % (corr_len * 100.0 / total_len))
                report.add_field(reporting.Fields.KMER_MIS_LENGTH, '%.2f' % (mis_len * 100.0 / total_len))
                report.add_field(reporting.Fields.KMER_UNDEF_LENGTH, '%.2f' % (undef_len * 100.0 / total_len))
                report.add_field(reporting.Fields.KMER_MISASSEMBLIES, misassemblies)
            checked_assemblies.append(contigs_fpath)

    contigs_fpaths = [fpath for fpath in contigs_fpaths if fpath not in checked_assemblies]
    if len(contigs_fpaths) == 0:
        logger.info('Done.')
        return

    if qconfig.platform_name == 'linux_32':
        logger.warning('  Sorry, can\'t run KMC on this platform, skipping...')
        return None

    kmc_dirpath = get_dir_for_download(kmc_dirname, 'KMC', ['kmc', 'kmc_tools'], logger)
    global kmc_bin_fpath
    global kmc_tools_fpath
    kmc_bin_fpath = download_external_tool('kmc', kmc_dirpath, 'KMC', platform_specific=True, is_executable=True)
    kmc_tools_fpath = download_external_tool('kmc_tools', kmc_dirpath, 'KMC', platform_specific=True, is_executable=True)
    if not exists(kmc_bin_fpath) or not exists(kmc_tools_fpath) or not compile_minimap(logger):
        logger.warning('  Sorry, can\'t run KMC, skipping...')
        return None

    logger.info('Running KMC on reference...')
    log_fpath = join(output_dir, 'kmc.log')
    err_fpath = join(output_dir, 'kmc.err')
    open(log_fpath, 'w').close()
    open(err_fpath, 'w').close()

    tmp_dirpath = join(output_dir, 'tmp')
    if not isdir(tmp_dirpath):
        os.makedirs(tmp_dirpath)
    ref_kmc_out_fpath = count_kmers(tmp_dirpath, ref_fpath, log_fpath, err_fpath)
    unique_kmers = get_kmers_cnt(tmp_dirpath, ref_kmc_out_fpath, log_fpath, err_fpath)
    if not unique_kmers:
        return

    logger.info('Analyzing assemblies completeness...')
    kmc_out_fpaths = []
    for contigs_fpath in contigs_fpaths:
        report = reporting.get(contigs_fpath)
        kmc_out_fpath = count_kmers(tmp_dirpath, contigs_fpath, log_fpath, err_fpath)
        intersect_out_fpath = intersect_kmers(tmp_dirpath, [ref_kmc_out_fpath, kmc_out_fpath], log_fpath, err_fpath)
        matched_kmers = get_kmers_cnt(tmp_dirpath, intersect_out_fpath, log_fpath, err_fpath)
        completeness = matched_kmers * 100.0 / unique_kmers
        report.add_field(reporting.Fields.KMER_COMPLETENESS, '%.2f' % completeness)
        kmc_out_fpaths.append(intersect_out_fpath)

    logger.info('Analyzing assemblies accuracy...')
    ref_contigs = [name for name, _ in read_fasta(ref_fpath)]
    ref_kmers, downsampled_kmers_fpath = downsample_kmers(tmp_dirpath, ref_fpath, ref_kmc_out_fpath, log_fpath, err_fpath)
    for contigs_fpath, kmc_db_fpath in zip(contigs_fpaths, kmc_out_fpaths):
        report = reporting.get(contigs_fpath)
        corr_len = None
        mis_len = None
        undef_len = None
        misassemblies = None
        total_len = 0
        contig_lens = dict()
        for name, seq in read_fasta(contigs_fpath):
            total_len += len(seq)
            contig_lens[name] = len(seq)

        if report.get_field(reporting.Fields.CONTIGS) > MAX_CONTIGS_NUM:
            logger.warning('Assembly is too fragmented. Scaffolding accuracy will not be assessed.')
        elif len(ref_contigs) > MAX_REF_CONTIGS_NUM:
            logger.warning('Reference is too fragmented. Scaffolding accuracy will not be assessed.')
        else:
            corr_len = 0
            mis_len = 0
            kmers_by_contig, kmers_pos_by_contig = align_kmers(tmp_dirpath, contigs_fpath, downsampled_kmers_fpath, err_fpath,
                                                               qconfig.max_threads)
            misassemblies = 0
            for contig in kmers_by_contig.keys():
                contig_markers = []
                translocations = 0
                relocations = 0
                prev_pos, prev_ref_pos, prev_chrom, marker = None, None, None, None
                for pos, kmer in sorted(zip(kmers_pos_by_contig[contig], kmers_by_contig[contig]), key=lambda x: x[0]):
                    ref_chrom, ref_pos = ref_kmers[kmer]
                    if prev_pos and prev_chrom:
                        if prev_chrom == ref_chrom and abs(abs(pos - prev_pos) / abs(ref_pos - prev_ref_pos) - 1) <= 0.05:
                            marker = (pos, ref_pos, ref_chrom)
                        elif marker:
                            contig_markers.append(marker)
                            pos, ref_pos, ref_chrom, marker = None, None, None, None
                    prev_pos, prev_ref_pos, prev_chrom = pos, ref_pos, ref_chrom
                if marker:
                    contig_markers.append(marker)
                prev_pos, prev_ref_pos, prev_chrom = None, None, None
                for marker in contig_markers:
                    pos, ref_pos, ref_chrom = marker
                    if prev_pos and prev_chrom:
                        if ref_chrom != prev_chrom:
                            translocations += 1
                        elif _get_dist_inconstistency(pos, prev_pos, ref_pos, prev_ref_pos) > EXT_RELOCATION_SIZE:
                            relocations += 1
                    prev_pos, prev_ref_pos, prev_chrom = pos, ref_pos, ref_chrom
                if translocations or relocations:
                    mis_len += contig_lens[contig]
                elif len(contig_markers) > 0:
                    corr_len += contig_lens[contig]
                misassemblies += (relocations + translocations)
                undef_len = total_len - corr_len - mis_len
            report.add_field(reporting.Fields.KMER_CORR_LENGTH, '%.2f' % (corr_len * 100.0 / total_len))
            report.add_field(reporting.Fields.KMER_MIS_LENGTH, '%.2f' % (mis_len * 100.0 / total_len))
            report.add_field(reporting.Fields.KMER_UNDEF_LENGTH, '%.2f' % (undef_len * 100.0 / total_len))
            report.add_field(reporting.Fields.KMER_MISASSEMBLIES, misassemblies)

        create_kmc_stats_file(output_dir, contigs_fpath, ref_fpath,
                              report.get_field(reporting.Fields.KMER_COMPLETENESS),
                              corr_len, mis_len, undef_len, total_len, misassemblies)
    save_kmers(output_dir)
    if not qconfig.debug:
        shutil.rmtree(tmp_dirpath)
    logger.info('Done.')
