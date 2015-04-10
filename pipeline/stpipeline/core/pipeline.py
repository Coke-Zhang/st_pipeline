#!/usr/bin/env python
""" 
This is the main API for the ST pipeline, it needs a bunch of files and parameters in order
to run the jobs, input files are fastq, output files are json. It logs status into a file.
"""

from stpipeline.common.utils import *
from stpipeline.core.mapping import *
from stpipeline.core.annotation import *
from stpipeline.common.json_utils import *
from stpipeline.common.fastq_utils import reformatRawReads
from stpipeline.common.sam_utils import sortSamFile, filterAnnotatedReads
import os
from glob import glob
import logging
import subprocess

class Pipeline():
    
    LogName = "STPipeline"
    DefaultLogLevel = 'DEBUG'
    
    def __init__(self):
        self.allowed_missed = 3
        self.allowed_kimera = 6
        self.overhang = 2
        self.min_length_trimming = 28
        self.trimming_fw = 42
        self.trimming_rv = 5
        self.min_quality_trimming = 20 
        self.clean = True
        self.barcode_start = 0
        self.barcode_length = 18
        self.threads = 8
        self.verbose = False
        self.ids = None
        self.ref_map = None
        self.ref_annotation = None
        self.expName = None
        self.htseq_mode = "intersection-nonempty"
        self.htseq_no_ambiguous = False
        self.qual64 = False
        self.contaminant_index = None
        self.fastq_fw = None
        self.fastq_rv = None
        self.path = None
        self.logger = None
        self.logfile = None
        self.output_folder = None
        self.temp_folder = None
        self.molecular_barcodes = False
        self.mc_allowed_missmatches = 1
        self.mc_start_position = 19
        self.mc_end_position = 27
        self.min_cluster_size = 2
        self.keep_discarded_files = False
        self.remove_polyA_distance = 0
        self.remove_polyT_distance = 0
        self.remove_polyG_distance = 0
        self.remove_polyC_distance = 0
        
    def sanityCheck(self):
        """ 
        Performs some basic sanity checks in the input paramters
        """

        conds = {"forward_file": fileOk(self.fastq_fw), "revers_file": fileOk(self.fastq_rv), 
                 "ids": fileOk(self.ids), "ref_annotation": fileOk(self.ref_annotation), 
                 "ref_mapping": self.ref_map is not None, "Exp Name":  self.expName is not None}
        
        conds["annotation_extension"] = self.ref_annotation.endswith("gtf")
        conds["annotation_mode"] = self.htseq_mode in ["union","intersection-nonempty","intersection-strict"]
        conds["forward_extension"] = self.fastq_fw.endswith("fastq") or self.fastq_fw.endswith("fq")
        conds["forward_extension"] = self.fastq_fw.endswith("fastq") or self.fastq_fw.endswith("fq")
        conds["polyA"] = self.remove_polyA_distance >= 0 
        conds["polyT"] = self.remove_polyT_distance >= 0 
        conds["polyG"] = self.remove_polyG_distance >= 0
        conds["polyC"] = self.remove_polyC_distance >= 0
        
        #TODO add much more checks for input parameters, specially integers
        
        if self.molecular_barcodes:
            conds["molecular_barcodes"] = self.mc_start_position > self.barcode_length \
                                        or self.mc_end_position > self.mc_start_position
        if not all(conds.values()):
            error = "Error: required file/s and or parameters not " \
            " found or incorrect parameters :" + str(conds)
            self.logger.error(error)
            raise RuntimeError(error)

        #test the presence of the scripts (only STAR for now)
        required_scripts = set(['STAR'])

        unavailable_scripts = set()
        for script in required_scripts:
            if which(script) is None: 
                unavailable_scripts.add(script)
         
        if len(unavailable_scripts) == 0:
            self.logger.info("All tools present..starting the analysis")
        else:
            error = "Error, these programs not found:\t".join(unavailable_scripts)
            self.logger.error(error)
            raise RuntimeError(error)
       
    def createParameters(self, parser):
            """
            Adds the pipeline's parameters to a given
            Argparse object 
            """
            parser.add_argument('fastq_files', nargs=2)
            parser.add_argument('--ids',
                                help='The name of the file containing the barcodes and the coordinates')
            parser.add_argument('--ref-map',
                                help="<path_to_genome_indexes> = Reference genome index " \
                                "for the genome that you want to use to align the reads")
            parser.add_argument('--ref-annotation',
                                help="Path to the reference annotation file " \
                                "(htseq requires a GTF file annotation file that you want to use to annotate")
            parser.add_argument('--expName', help="Name of the experiment (output file name)")
            parser.add_argument('--allowed-missed', default=6, 
                                help="Number of allowed mismatches when mapping against the barcodes")
            parser.add_argument('--allowed-kimer', default=7, 
                                help="KMer length when mapping against the barcodes")
            parser.add_argument('--overhang', default=2,
                                help="Extra flanking bases added when mapping against the barcodes")
            parser.add_argument('--min-length-qual-trimming', default=28,
                                help="Minimum length of the sequence for mapping after trimming, " \
                                "shorter reads will be discarded")
            parser.add_argument('--mapping-fw-trimming', default=42,
                                help="Number of bases to trim in the forward reads for the Mapping [24 + ID_LENGTH]")
            parser.add_argument('--mapping-rv-trimming', default=5,
                                help="Number of bases to trim in the reverse reads for the Mapping")
            parser.add_argument('--length-id', default=18, help="Length of IDs (the length of the barcodes)")
            parser.add_argument('--contaminant-index',
                                help="<path_to_genome_indexes> = When provided, reads will be filtered "
                                "against the specified genome index, non-mapping reads will be saved and demultiplexed")
            parser.add_argument('--qual-64', action="store_true", default=False,
                                help="Use phred-64 quality instead of phred-33(default)")
            parser.add_argument('--htseq-mode', default="intersection-nonempty",
                                help="Mode of Annotation when using HTSeq. "
                                "Modes = {union,intersection-nonempty(default),intersection-strict}")
            parser.add_argument('--htseq-no-ambiguous', action="store_true",
                                help="When using htseq discard reads annotating ambiguous genes")
            parser.add_argument('--start-id', default=0, help="Start position of the IDs (Barcodes) in the reads")
            parser.add_argument('--no-clean-up', action="store_false", default=True,
                                help="Do not remove temporary files at the end (useful for debugging)")
            parser.add_argument('--verbose', action="store_true", default=False,
                                help="Show extra information on the log")
            parser.add_argument('--mapping-threads', default=8, help="Number of threads to use in the mapping step")
            parser.add_argument('--min-quality-trimming', default=20, help="Minimum quality for trimming")
            parser.add_argument('--bin-path', 
                                help="Path to folder where binary executables are present (system path by default)")
            parser.add_argument('--log-file', 
                                help="Name of the file that we want to use to store the logs (default output to screen)")
            parser.add_argument('--output-folder', help='Path of the output folder')
            parser.add_argument('--temp-folder', help='Path of the location for temporary files')
            parser.add_argument('--molecular-barcodes',
                                action="store_true", help="Activates the molecular barcodes PCR duplicates filter")
            parser.add_argument('--mc-allowed-missmatches', default=1,
                                help='Number of allowed missmatches when applying the molecular barcodes PCR filter')
            parser.add_argument('--mc-start-position', type=int, default=19,
                                help='Position (base wise) of the first base of the molecular barcodes')
            parser.add_argument('--mc-end-position', default=27,
                                help='Position (base wise) of the last base of the molecular barcodes')
            parser.add_argument('--min-cluster-size', default=2,
                                help='Min number of equal molecular barcodes to count as a cluster')
            parser.add_argument('--keep-discarded-files', action="store_true", default=False,
                                help='Writes down discarded reads and barcodes into files')
            parser.add_argument('--remove-polyA', default=0, 
                                help="Remove PolyAs and everything after it in the reads of a length at least as given number")
            parser.add_argument('--remove-polyT', default=0, 
                                help="Remove PolyTs and everything after it in the reads of a length at least as given number")
            parser.add_argument('--remove-polyG', default=0, 
                                help="Remove PolyGs and everything after it in the reads of a length at least as given number")
            parser.add_argument('--remove-polyC', default=0,
                                help="Remove PolyCs and everything after it in the reads of a length at least as given number")   
            return parser
         
    def load_parameters(self, options):
        """
        Initialize logger, load up some parameters
        and prints out some information
        """
    
        #init pipeline arguments
        self.mapper = options.mapper
        self.allowed_missed = int(options.allowed_missed)
        self.allowed_kimera = int(options.allowed_kimer)
        self.overhang = int(options.overhang)
        self.min_length_trimming = int(options.min_length_qual_trimming)
        self.trimming_fw = int(options.mapping_fw_trimming)
        self.trimming_rv = int(options.mapping_rv_trimming)
        self.min_quality_trimming = int(options.min_quality_trimming) 
        self.clean = options.no_clean_up
        self.barcode_start = int(options.start_id)
        self.barcode_length = int(options.length_id)
        self.threads = int(options.mapping_threads)
        self.verbose = options.verbose
        self.ids = os.path.abspath(options.ids)
        self.ref_map = os.path.abspath(options.ref_map)
        self.ref_annotation = os.path.abspath(options.ref_annotation)
        self.expName = options.expName
        self.htseq_mode = options.htseq_mode
        self.htseq_no_ambiguous = options.htseq_no_ambiguous
        self.qual64 = options.qual_64
        self.contaminant_index = options.contaminant_index
        self.path = options.bin_path
        if options.log_file is not None:
            self.logfile = os.path.abspath(options.log_file)    
        self.fastq_fw = options.fastq_files[0]
        self.fastq_rv = options.fastq_files[1]
        if options.output_folder is not None and os.path.isdir(options.output_folder):
            self.output_folder = os.path.abspath(options.output_folder)
        if options.temp_folder is not None and os.path.isdir(options.temp_folder): 
            self.temp_folder = os.path.abspath(options.temp_folder)
        self.molecular_barcodes = options.molecular_barcodes
        self.mc_allowed_missmatches = int(options.mc_allowed_missmatches)
        self.mc_start_position = int(options.mc_start_position)
        self.mc_end_position = int(options.mc_end_position)
        self.min_cluster_size = int(options.min_cluster_size)
        self.keep_discarded_files = options.keep_discarded_files
        self.remove_polyA_distance = int(options.remove_polyA)
        self.remove_polyT_distance = int(options.remove_polyT)
        self.remove_polyG_distance = int(options.remove_polyG)
        self.remove_polyC_distance = int(options.remove_polyC)
        
    def createLogger(self):
        """
        Creates a logging object and logs some information about parameters
        """    
        # create a logger
        if self.logfile is not None:
            logging.basicConfig(filename=self.logfile ,level=logging.DEBUG)
        else:
            logging.basicConfig(level=logging.DEBUG)
        self.logger = logging.getLogger(self.__class__.LogName)
        
        #load the given path into the system PATH
        if self.path is not None and os.path.isdir(self.path): 
            os.environ["PATH"] += os.pathsep + self.path

        # Set output and temp folders if erroneous
        if self.output_folder is None or not os.path.isdir(self.output_folder):
            self.logger.info("Invalid path for output directory -- using current directory instead")
            self.output_folder = os.path.abspath(os.getcwd())
        if self.temp_folder is None or not os.path.isdir(self.temp_folder):
            self.logger.info("Invalid path for temp directory -- using current directory instead")
            self.temp_folder = os.path.abspath(os.getcwd())
            
        self.logger.info("Output directory : " + self.output_folder)
        self.logger.info("Temp directory : " + self.temp_folder)
        self.logger.info("Experiment : " + str(self.expName))
        self.logger.info("Forward reads file : " + str(self.fastq_fw))
        self.logger.info("Reverse reads file : " + str(self.fastq_rv))
        self.logger.info("Ids file : " + str(self.ids))
        self.logger.info("Reference mapping file : " + str(self.ref_map))
        self.logger.info("Reference annotation file : " + str(self.ref_annotation))
        if(self.contaminant_index):
            self.logger.info("Using contamination filter with " + str(self.contaminant_index))
        self.logger.info("Nodes : " + str(self.threads))
        self.logger.info("Main parameters: ")
        self.logger.info("TaggD allowed missmatches " + str(self.allowed_missed))
        self.logger.info("TaggD barcode legnth " + str(self.barcode_length))
        self.logger.info("TaggD kmer size " + str(self.allowed_kimera))
        self.logger.info("TaggD overhang " + str(self.overhang))
        self.logger.info("Mapping forward trimming " + str(self.trimming_fw))
        self.logger.info("Mapping reverse trimming " + str(self.trimming_rv))
        self.logger.info("Mapper : STAR")
        self.logger.info("Annotation Tool : HTSeq")

        if self.molecular_barcodes:
            self.logger.info("Using Molecular Barcodes")
            self.logger.info("Molecular Barcode start position " + str(self.mc_start_position))
            self.logger.info("Molecular Barcode end position " + str(self.mc_end_position))
            self.logger.info("Molecular Barcode min cluster size " + str(self.min_cluster_size))
            self.logger.info("Molecular Barcode allowed missmatches " + str(self.mc_allowed_missmatches))
            
        if self.remove_polyA_distance > 0:
            self.logger.info("Removing polyA adaptors of a length at least " + str(self.remove_polyA_distance))                        
        if self.remove_polyT_distance > 0:
            self.logger.info("Removing polyT adaptors of a length at least " + str(self.remove_polyT_distance))
        if self.remove_polyG_distance > 0:
            self.logger.info("Removing polyG adaptors of a length at least " + str(self.remove_polyG_distance))
        if self.remove_polyC_distance > 0:
            self.logger.info("Removing polyC adaptors of a length at least " + str(self.remove_polyC_distance))

    def run(self):
        """ 
        Runs the whole pipeline given the parameters present
        """
        globaltime = TimeStamper()

        #=================================================================
        # START PIPELINE
        #=================================================================
        start_exe_time = globaltime.getTimestamp()
        self.logger.info("Starting the pipeline : " + str(start_exe_time))

        #=================================================================
        # STEP: add BC and PolyT from FW reads to the RW reads and apply quality filter
        # also applies quality trimming and adaptor removal
        #=================================================================
        #NOTE after trimming both forward and reverse reads will have their trimming part remove
        #on top of the quality trimmed based removed BUT the barcode will be re-attached to both of them
        fastq_fw_trimmed, fastq_rv_trimmed = reformatRawReads(self.fastq_fw,
                                                              self.fastq_rv,
                                                              self.barcode_length,
                                                              self.trimming_fw,
                                                              self.trimming_rv,
                                                              self.min_quality_trimming,
                                                              self.min_length_trimming,
                                                              self.remove_polyA_distance,
                                                              self.remove_polyT_distance,
                                                              self.remove_polyG_distance,
                                                              self.remove_polyC_distance,
                                                              self.qual64, 
                                                              self.temp_folder,
                                                              self.keep_discarded_files)
        
        #=================================================================
        # CONDITIONAL STEP: Filter out contaminated reads, e.g. typically bacterial rRNA
        #=================================================================
        if self.contaminant_index:
            # To remove contaminants sequence with align the reads to the contaminant genome
            # and keep the un-mapped reads
            # We do pair end alignment, trimming is the same for forward and reverse
            # which is just the barcode length
            contaminated_sam, fastq_fw_trimmed, fastq_rv_trimmed = alignReads(fastq_fw_trimmed,
                                                                              fastq_rv_trimmed,
                                                                              self.contaminant_index,
                                                                              self.barcode_length,
                                                                              self.barcode_length,
                                                                              self.threads,
                                                                              self.temp_folder)
                        
            if not self.keep_discarded_files: safeRemove(contaminated_sam)
            
        #=================================================================
        # STEP: maps against the genome using STAR
        #=================================================================
        # We do pair end alignment, trimming is the same for forward and reverse
        # which is just the barcode length
        sam_mapped, unmapped_forward, unmapped_reverse = alignReads(fastq_fw_trimmed,
                                                                    fastq_rv_trimmed,
                                                                    self.ref_map,
                                                                    self.barcode_length,
                                                                    self.barcode_length, 
                                                                    self.threads,
                                                                    self.temp_folder)
        if self.clean: 
            safeRemove(fastq_fw_trimmed)
            safeRemove(fastq_rv_trimmed)
        if not self.keep_discarded_files: 
            safeRemove(unmapped_forward)
            safeRemove(unmapped_reverse)
         
        #=================================================================
        # STEP: SORT sam file with mapped reads by coordinate position
        #=================================================================
        sam_sorted = sortSamFile(sam_mapped, self.temp_folder)
        if self.clean: safeRemove(sam_mapped)
        
        #=================================================================
        # STEP: annotate using htseq count
        #=================================================================
        annotatedFile = annotateReads(sam_sorted,
                                      self.ref_annotation,
                                      self.htseq_mode,
                                      self.temp_folder)
        if self.clean: safeRemove(sam_sorted)

        #=================================================================
        # STEP: filter out un-annotated reads
        #=================================================================
        annotatedFilteredFile = filterAnnotatedReads(annotatedFile, 
                                                     self.htseq_no_ambiguous,
                                                     self.temp_folder, 
                                                     self.keep_discarded_files)

        if self.clean: safeRemove(annotatedFilteredFile)
        
        #=================================================================
        # STEP: Map against the barcodes
        #=================================================================
        mapFile = barcodeDemultiplexing(annotatedFilteredFile,
                                        self.ids, 
                                        self.allowed_missed, 
                                        self.allowed_kimera, 
                                        self.barcode_start,  
                                        self.overhang,
                                        self.temp_folder,
                                        self.keep_discarded_files)
        if self.clean: safeRemove(annotatedFilteredFile)

        #=================================================================
        # STEP: create json files with the results
        #=================================================================
        self.createDataset(mapFile,
                           self.expName,
                           self.barcode_length,
                           self.molecular_barcodes,
                           self.mc_allowed_missmatches,
                           self.mc_start_position,
                           self.mc_end_position,
                           self.min_cluster_size)
        if self.clean: safeRemove(mapFile)

        #=================================================================
        # END PIPELINE
        #=================================================================
        finish_exe_time = globaltime.getTimestamp()
        total_exe_time = finish_exe_time - start_exe_time
        self.logger.info("Total Execution Time : " + str(total_exe_time))

    def createDataset(self, input_name, output_name, trim_bases = 42, 
                      molecular_barcodes = False, allowed_missmatches = 1, 
                      start_position = 19, end_position = 30, min_cluster_size = 2):
        """ 
        parse annotated and mapped reads with the reads that contain barcodes to
        create json files with the barcodes and coordinates and json file with the raw reads
        and some useful stats and plots
        It also allows to remove PCR Duplicates using molecular barcodes
        We passes the number of forward bases trimmed for mapping to get a clean read
        in the output
        """
        
        self.logger.info("Start Creating dataset")
        
        args = ['createDataset.py', '--input', str(input_name), 
                '--output-name', output_name, '--trim-bases', trim_bases]
        
        if molecular_barcodes:
            args += ['--molecular-barcodes', '--mc-allowed-missmatches', allowed_missmatches, 
                '--mc-start-position', start_position, '--mc-end-position', end_position, 
                '--min-cluster-size', min_cluster_size]
            
        if self.output_folder is not None:
            args += ['--output-folder', self.output_folder]
     
        try:
            proc = subprocess.Popen([str(i) for i in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (stdout, errmsg) = proc.communicate()
        except Exception as e:
            error = "Error creating dataset: createDataset execution failed"
            self.logger.info(error)
            self.logger.info(e)
            raise
        
        if len(errmsg) > 0:
            error = "Error, There was an error creating the dataset: \n" + stdout + " \n" + errmsg
            self.logger.error(error)
            raise RuntimeError(error)    
              
        procOut = stdout.split("\n")
        self.logger.info('Creating dataset stats :')
        for line in procOut: 
            self.logger.info(str(line))
        self.logger.info("Finish Creating dataset")
