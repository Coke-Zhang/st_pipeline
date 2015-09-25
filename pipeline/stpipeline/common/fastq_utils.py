#!/usr/bin/env python
""" 
This module contains some functions to deal with fastq files
"""

from stpipeline.common.utils import *
from stpipeline.common.adaptors import removeAdaptor
import logging 
from stpipeline.common.stats import Stats
from itertools import izip

def coroutine(func):
    """ 
    Coroutine decorator, starts coroutines upon initialization.
    """
    def start(*args, **kwargs):
        cr = func(*args, **kwargs)
        cr.next()
        return cr
    return start

def quality_trim_index(qualities, cutoff, base=33):
    """
    NOTE : function snippet from CutAdapt 
    https://code.google.com/p/cutadapt/
    
    Find the position at which to trim a low-quality end from a nucleotide sequence.

    Qualities are assumed to be ASCII-encoded as chr(qual + base).

    The algorithm is the same as the one used by BWA within the function
    'bwa_trim_read':
    - Subtract the cutoff value from all qualities.
    - Compute partial sums from all indices to the end of the sequence.
    - Trim sequence at the index at which the sum is minimal.
    """
    s = 0
    max_qual = 0
    max_i = len(qualities)
    for i in reversed(xrange(max_i)):
        q = ord(qualities[i]) - base
        s += cutoff - q
        if s < 0:
            break
        if s > max_qual:
            max_qual = s
            max_i = i
    return max_i

def trim_quality(record, trim_distance, min_qual=20, 
                 min_length=28, qual64=False):    
    """
    :param record the fastq read (name,sequence,quality)
    :param trim_distance the number of bases to be trimmed (not considered)
    :param min_qual the quality threshold to trim
    :param min_length the min length of a valid read after trimming
    :param qual64 true of the qualities are in phred64 format
    Perfoms a bwa-like quality trimming on the sequence and 
    quality in tuple record(name,seq,qual)
    Returns the trimmed read or None if the read has to be discarded
    """
    qscore = record[2][trim_distance:]
    sequence = record[1][trim_distance:]
    num_bases_trimmed = len(sequence)
    name = record[0]
    phred = 64 if qual64 else 33
    
    # Get the position at which to trim
    cut_index = quality_trim_index(qscore, min_qual, phred)
    # Get the number of bases suggested to trim
    nbases = num_bases_trimmed - cut_index
    
    # Check if the trimmed sequence would have min length (at least)
    # if so return the trimmed read otherwise return None
    if (num_bases_trimmed - nbases) >= min_length:
        new_seq = record[1][:(num_bases_trimmed - nbases)]
        new_qual = record[2][:(num_bases_trimmed - nbases)]
        return name, new_seq, new_qual
    else:
        return None
    
def getFake(header, num_bases):
    """ 
    Generates a fake fastq record(name,seq,qual) from header and length
    given as input
    """
    new_seq = "".join("N" for k in xrange(num_bases))
    new_qual = "".join("B" for k in xrange(num_bases))
    return header, new_seq, new_qual

def readfq(fp): # this is a generator function
    """ 
    Heng Li's fasta/fastq reader function.
    """
    last = None # this is a buffer keeping the last unprocessed line
    while True: # mimic closure; is it a bad idea?
        if not last: # the first record or a record following a fastq
            for l in fp: # search for the start of the next record
                if l[0] in '>@': # fasta/q header line
                    last = l[:-1] # save this line
                    break
        if not last: break
        #name, seqs, last = last[1:].partition(" ")[0], [], None
        name, seqs, last = last[1:], [], None
        for l in fp: # read the sequence
            if l[0] in '@+>':
                last = l[:-1]
                break
            seqs.append(l[:-1])
        if not last or last[0] != '+': # this is a fasta record
            yield name, ''.join(seqs), None # yield a fasta record
            if not last: break
        else: # this is a fastq record
            seq, leng, seqs = ''.join(seqs), 0, []
            for l in fp: # read the quality
                seqs.append(l[:-1])
                leng += len(l) - 1
                if leng >= len(seq):  # have read enough quality
                    last = None
                    yield name, seq, ''.join(seqs)  # yield a fastq record
                    break
            if last:  # reach EOF before reading enough quality
                yield name, seq, None  # yield a fasta record instead
                break

@coroutine
def writefq(fp):  # This is a coroutine
    """ 
    Fastq writing generator sink.
    Send a (header, sequence, quality) triple to the instance to write it to
    the specified file pointer.
    """
    fq_format = '@{header}\n{sequence}\n+\n{quality}\n'
    try:
        while True:
            record = yield
            read = fq_format.format(header=record[0], sequence=record[1], quality=record[2])
            fp.write(read)
    except GeneratorExit:
        return
  
def filter_rRNA_reads(forward_reads, reverse_reads, qa_stats, outputFolder=None):
    """
    :param forward_reads reads coming from un-aligned in STAR (rRNA filter)
    :param reverse_reads reads coming from un-aligned in STAR (rRNA filter)
    :param outputFolder optional folder to output files
    Very annoying but STAR when outputs un-aligned reads, it outputs all the reads
    and adds a field to the header (00 for unaligned and 01 for aligned)
    We use STAR for the rRNA filter before mapping so this function is needed
    to extract un-aligned reads from all the reads. This function will only
    work when used after the rRNA filter step. It returns the sub-set of un-aligned
    reads
    """
    logger = logging.getLogger("STPipeline")
    logger.info("Start filtering rRNA un-mapped reads")
    
    out_rw = 'R2_rRNA_filtered_clean.fastq'
    out_fw = 'R1_rRNA_filtered_clean.fastq'
    
    if outputFolder is not None and os.path.isdir(outputFolder):
        out_rw = os.path.join(outputFolder, out_rw)
        out_fw = os.path.join(outputFolder, out_fw)
    
    out_fw_handle = safeOpenFile(out_fw, 'w')
    out_fw_writer = writefq(out_fw_handle)
    out_rw_handle = safeOpenFile(out_rw, 'w')
    out_rw_writer = writefq(out_rw_handle)
    fw_file = safeOpenFile(forward_reads, "rU")
    rw_file = safeOpenFile(reverse_reads, "rU")
    
    # If the line contains a 00 according to STAR it is un-mapped
    # We write a fake sequence for the mapped ones (the one that are contaminated)
    contaminated_fw = 0
    contaminated_rv = 0
    for line1, line2 in izip(readfq(fw_file), readfq(rw_file)):
        header_fw = line1[0]
        header_rv = line2[0]
        
        if header_fw.split()[1] == "00":
            out_fw_writer.send(line1)
        else:
            contaminated_fw += 1
            out_fw_writer.send(getFake(header_fw, len(line1[1])))
            
        if header_rv.split()[1] == "00":
            out_rw_writer.send(line2)
        else:
            contaminated_rv += 1
            out_rw_writer.send(getFake(header_rv, len(line2[1])))
   
    out_fw_writer.close()
    out_rw_writer.close()
    out_fw_handle.close()
    out_rw_handle.close()
    fw_file.close()
    rw_file.close()
    
    if not fileOk(out_fw) or not fileOk(out_rw):
        error = "Error filtering rRNA un-mapped, output file is not present %s,%s" % (out_fw,)
        logger.error(error)
        raise RuntimeError(error + "\n")
    
    # Add stats to QA Stats object
    qa_stats.reads_after_rRNA_trimming = (qa_stats.reads_after_trimming_forward 
                                          + qa_stats.reads_after_trimming_reverse) \
                                          - (contaminated_fw + contaminated_rv)
    logger.info("Finish filtering rRNA un-mapped reads")
    return out_fw, out_rw
      
def reverse_complement(seq):
    """
    :param seq a FASTQ sequence
    This functions returns the reverse complement
    of the sequence given as input
    """
    alt_map = {'ins':'0'}
    complement = {'A': 'T', 'C': 'G', 'G': 'C', 'T': 'A'}   
    for k,v in alt_map.iteritems():
        seq = seq.replace(k,v)
    bases = list(seq) 
    bases = reversed([complement.get(base,base) for base in bases])
    bases = ''.join(bases)
    for k,v in alt_map.iteritems():
        bases = bases.replace(v,k)
    return bases
  
def reformatRawReads(fw, 
                     rw, 
                     qa_stats,
                     barcode_start=0, 
                     barcode_length=18,
                     filter_AT_content=90,
                     molecular_barcodes=False, 
                     mc_start=18, 
                     mc_end=27,
                     trim_fw=42, 
                     trim_rw=5,
                     min_qual=20, 
                     min_length=28,
                     polyA_min_distance=0, 
                     polyT_min_distance=0, 
                     polyG_min_distance=0, 
                     polyC_min_distance=0,
                     qual64=False, 
                     outputFolder=None, 
                     keep_discarded_files=False):
    """ 
    :param fw the fastq file with the forward reads
    :param rw the fastq file with the reverse reads
    :param barcode_start the base where the barcode sequence starts
    :param barcode_length the number of bases of the barcodes
    :param molecular_barcodes if True the forward reads contain molecular barcodes
    :param mc_start the start position of the molecular barcodes if any
    :param mc_end the end position of the molecular barcodes if any
    :param trim_fw how many bases we want to trim (not consider in the forward)
    :param trim_rw how many bases we want to trim (not consider in the reverse)
    :param min_qual the min quality value to use to trim quality
    :param min_length the min valid length for a read after trimming
    :param polyA_min_distance if >0 we remove PolyA adaptors from the reads
    :param polyT_min_distance if >0 we remove PolyT adaptors from the reads
    :param polyG_min_distance if >0 we remove PolyG adaptors from the reads
    :param qual64 true of qualities are in phred64 format
    :param outputFolder optional folder to output files
    :param keep_discarded_files when true files containing the discarded reads will be created
    This function does three things (all here for speed optimization)
      - It appends the barcode and the molecular barcode (if any)
        from forward reads to reverse reads
      - It performs a BWA quality trimming discarding very short reads
      - It removes adaptors from the reads (optional)
    """
    logger = logging.getLogger("STPipeline")
    logger.info("Start Reformatting and Filtering raw reads")
    
    out_rw = 'R2_trimmed_formated.fastq'
    out_fw = 'R1_trimmed_formated.fastq'
    out_fw_discarded = 'R1_trimmed_formated_discarded.fastq'
    out_rw_discarded = 'R2_trimmed_formated_discarded.fastq'
    
    if outputFolder is not None and os.path.isdir(outputFolder):
        out_rw = os.path.join(outputFolder, out_rw)
        out_fw = os.path.join(outputFolder, out_fw)
        out_fw_discarded = os.path.join(outputFolder, out_fw_discarded)
        out_rw_discarded = os.path.join(outputFolder, out_rw_discarded)
    
    out_fw_handle = safeOpenFile(out_fw, 'w')
    out_fw_writer = writefq(out_fw_handle)
    out_rw_handle = safeOpenFile(out_rw, 'w')
    out_rw_writer = writefq(out_rw_handle)

    if keep_discarded_files:
        out_rw_handle_discarded = safeOpenFile(out_rw_discarded, 'w')
        out_rw_writer_discarded = writefq(out_rw_handle_discarded)
        out_fw_handle_discarded = safeOpenFile(out_fw_discarded, 'w')
        out_fw_writer_discarded = writefq(out_fw_handle_discarded)
    
    # Open fastq files with the fastq parser
    fw_file = safeOpenFile(fw, "rU")
    rw_file = safeOpenFile(rw, "rU")

    # Some counters
    total_reads = 0
    dropped_fw = 0
    dropped_rw = 0
    
    # Build fake adpators with the parameters given
    adaptorA = "".join("A" for k in xrange(polyA_min_distance))
    adaptorT = "".join("T" for k in xrange(polyT_min_distance))
    adaptorG = "".join("G" for k in xrange(polyG_min_distance))
    adaptorC = "".join("C" for k in xrange(polyG_min_distance))
    
    iscorrect_mc = molecular_barcodes
    if mc_start < (barcode_start + barcode_length) \
    or mc_end < (barcode_start + barcode_length):
        logger.warning("Your molecular barcodes sequences overlap with the barcodes sequences")
        iscorrect_mc = False
        
    for line1, line2 in izip(readfq(fw_file), readfq(rw_file)):
        
        if line1 is None or line2 is None:
            logger.error("The input files %s,%s are not of the same length" % (fw,rw))
            break
        
        header_fw = line1[0]
        header_rv = line2[0]
        sequence_fw = line1[1]
        num_bases_fw = len(sequence_fw)
        sequence_rv = line2[1]
        num_bases_rv = len(sequence_rv)
        quality_fw = line1[2]
        quality_rv = line2[2]
        
        if header_fw.split()[0] != header_rv.split()[0]:
            logger.warning("Pair reads found with different names %s and %s" % (header_fw,header_rv))
        
        total_reads += 1
        
        # Get the barcode and molecular barcode if any from the forward read
        # to be attached to the reverse read
        to_append_sequence = sequence_fw[barcode_start:barcode_length]
        to_append_sequence_quality = quality_fw[barcode_start:barcode_length]
        if iscorrect_mc:
            to_append_sequence += sequence_fw[mc_start:mc_end]
            to_append_sequence_quality += quality_fw[mc_start:mc_end]
        
        # If read - trimming is not long enough or has a high AT content discard...
        if (num_bases_fw - trim_fw) < min_length or \
        ((sequence_fw.count("A") + sequence_fw.count("T")) / num_bases_fw) * 100 >= filter_AT_content:
            line1 = None
        if (num_bases_rv - trim_rw) < min_length or \
        ((sequence_rv.count("A") + sequence_rv.count("T")) / num_bases_rv) * 100 >= filter_AT_content:
            line2 = None
              
        # if indicated we remove the adaptor PolyA from both reads
        if polyA_min_distance > 0:
            line1 = removeAdaptor(line1, adaptorA, trim_fw, "5")
            line2 = removeAdaptor(line2, adaptorA, trim_rw, "5")
            
        # if indicated we remove the adaptor PolyT from both reads
        if polyT_min_distance > 0:
            line1 = removeAdaptor(line1, adaptorT, trim_fw, "5")
            line2 = removeAdaptor(line2, adaptorT, trim_rw, "5")
       
        # if indicated we remove the adaptor PolyG from both reads
        if polyG_min_distance > 0:
            line1 = removeAdaptor(line1, adaptorG, trim_fw, "5")
            line2 = removeAdaptor(line2, adaptorG, trim_rw, "5")
        
        # if indicated we remove the adaptor PolyC from both reads
        if polyC_min_distance > 0:
            line1 = removeAdaptor(line1, adaptorC, trim_fw, "5")
            line2 = removeAdaptor(line2, adaptorC, trim_rw, "5")
          
        line2_trimmed = None
        line1_trimmed = None
             
        # Trim reverse read
        if line2 is not None:
            line2_trimmed = trim_quality(line2, trim_rw, min_qual, min_length, qual64)
            
        # Trim forward
        if line1 is not None:
            line1_trimmed = trim_quality(line1, trim_fw, min_qual, min_length, qual64)
        
        if line1_trimmed is not None:
            out_fw_writer.send(line1_trimmed)
        else:
            # Write fake sequence so mapping wont fail for having reads with different lengths
            out_fw_writer.send(getFake(header_fw, num_bases_fw))
            dropped_fw += 1
            if keep_discarded_files:
                # Write to discarded the original record
                out_fw_writer_discarded.send((header_fw, sequence_fw, quality_fw))

        if line2_trimmed is not None:
            # Attach the barcode from the forward read only if the reverse read has not been completely trimmed
            new_seq = to_append_sequence + line2_trimmed[1]
            new_qual = to_append_sequence_quality + line2_trimmed[2]
            out_rw_writer.send((line2_trimmed[0], new_seq, new_qual))
        else:
            # Write fake sequence so mapping wont fail for having reads with different lengths
            out_rw_writer.send(getFake(header_rv, num_bases_rv))
            dropped_rw += 1  
            if keep_discarded_files:
                # Write to discarded the original record
                out_rw_writer_discarded.send((header_rv, sequence_rv, quality_rv))
    
    out_fw_writer.close()
    out_rw_writer.close()
    out_fw_handle.close()
    out_rw_handle.close()
    if keep_discarded_files:
        out_fw_handle_discarded.close()
        out_rw_handle_discarded.close()
    fw_file.close()
    rw_file.close()
    
    if not fileOk(out_fw) or not fileOk(out_rw):
        error = "Error reformatting raw reads: output file not present/s %s , %s" % (out_fw , out_rw)
        logger.error(error)
        raise RuntimeError(error + "\n")
    else:
        logger.info("Trimming stats total reads (pair): %s" % (str(total_reads)))
        logger.info("Trimming stats forward: %s reads have been dropped!" % (str(dropped_fw)))
        perc1 = '{percent:.2%}'.format(percent= float(dropped_fw) / float(total_reads))
        logger.info("Trimming stats forward: you just lost about %s of your data" % (perc1))
        logger.info("Trimming stats reverse: %s reads have been dropped!" % (str(dropped_rw))) 
        perc2 = '{percent:.2%}'.format(percent= float(dropped_rw) / float(total_reads) )
        logger.info("Trimming stats reverse: you just lost about %s of your data" % (perc2))
        logger.info("Trimming stats reads (forward) remaining: %s" % (str(total_reads - dropped_fw)))
        logger.info("Trimming stats reads (reverse) remaining: %s" % (str(total_reads - dropped_rw)))
        
        # Adding stats to QA Stats object
        qa_stats.input_reads_forward = total_reads
        qa_stats.input_reads_reverse = total_reads
        qa_stats.reads_after_trimming_forward = total_reads - dropped_fw
        qa_stats.reads_after_trimming_reverse = total_reads - dropped_rw
        
    logger.info("Finish Reformatting and Filtering raw reads")
    return out_fw, out_rw


