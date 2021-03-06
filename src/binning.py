#!/usr/bin/env python3
import sys
def eprint(*args, **kwargs): print(*args, file=sys.stderr, **kwargs)

#### Import some standard modules
import os
import argparse
import os.path
import timeit
import re
import json
import numpy as np
import gzip
from pyteomics import mzml, auxiliary


####################################################################################################
#### mzML Assessor class
class RepresentativeSpectrumCreator:


    ####################################################################################################
    #### Constructor
    def __init__(self, verbose=None):

        #### Set verbosity
        if verbose is None: verbose = 0
        self.verbose = verbose


    ####################################################################################################
    #### Read cluster list
    def read_cluster_list(self,file):

        clusters = []
        cluster = []
        icluster = 0

        with open(file) as infile:
            for line in infile:
                line = line.rstrip()
                columns = line.split()
                if len(columns) == 0:
                    clusters.append(cluster)
                    icluster += 1
                    #print(f"Cluster {icluster} has {len(cluster)} spectra")
                    cluster = []
                    continue
                cluster.append(columns[1])

        return(clusters)


    ####################################################################################################
    #### Read spectra
    def read_spectra(self,mzml_file,scan_list):

        #### Set up information
        t0 = timeit.default_timer()
        stats = { 'n_spectra': 0, 'n_ms1_spectra': 0, 'n_ms2_spectra': 0, 'n_HCD_spectra': 0, 'n_IT_spectra': 0, 'n_ETD_spectra': 0,
            'high_accuracy_precursors': 'unknown', 'fragmentation_type': 'unknown' }

        #### Show information
        n_scans = len(scan_list)
        if self.verbose >= 1:
            eprint(f"INFO: Reading {n_scans} scans from mzML file {mzml_file}")
            progress_intro = False

        #### Put spectra in a list
        spectra = []

        #### If the mzML is gzipped, then open with zlib, else a plain open
        match = re.search('\.gz$',mzml_file)
        if match:
            infile = gzip.open(mzml_file)
        else:
            infile = open(mzml_file, 'rb')

        #### Read spectra from the file
        with mzml.read(infile) as reader:

            for scan in scan_list:
                spectrum = reader.get_by_id(f"controllerType=0 controllerNumber=1 scan={scan}")

                #### Testing. Print the data structure of the first spectrum
                #if stats['n_spectra'] == 0:
                #    auxiliary.print_tree(spectrum)

                #### Set a default spectrum type
                spectrum_type = 'default'
                filter_string = None

                #### If the ms level is 2, then examine it for information
                if spectrum['ms level'] == 2 and 'm/z array' in spectrum:
                    precursor_mz = spectrum['precursorList']['precursor'][0]['selectedIonList']['selectedIon'][0]['selected ion m/z']
                    precursor_charge = spectrum['precursorList']['precursor'][0]['selectedIonList']['selectedIon'][0]['charge state']
                    print(f"INFO: Reading {scan}. Precursor m/z = {precursor_mz}. n peaks={len(spectrum['m/z array'])}")
                    peaklist = {
                        'm/z array': spectrum['m/z array'],
                        'intensity array': spectrum['intensity array'],
                        'precursor mz': precursor_mz,
                        'precursor charge': precursor_charge,
                    }
                    spectra.append(peaklist)
                else:
                    print(f"ERROR: scan {scan} is not ms_level=2! Skipping")

                #### Update counters and print progress
                stats['n_spectra'] += 1

        infile.close()
        if self.verbose >= 1: eprint("")

        #### Print final timing information
        t1 = timeit.default_timer()
        print(f"INFO: Read {stats['n_spectra']} spectra from {mzml_file}")
        print(f"INFO: Elapsed time: {t1-t0}")
        print(f"INFO: Processed {stats['n_spectra']/(t1-t0)} spectra per second")
        return(spectra)


    def read_spectra_clustered_mgf(self, clustered_mgf_file):
        """
        Read clustered MGF file and return clusters object
        
        clusters: dict of cluster_id -> peaklists
        peaklists: list of peaklist dicts
        peaklist: dict with 'm/z array', 'intensity array', 'cluster_id', 'spectrum_usi'
        precursor mz and precursor mass
        """
        all_spectra = []
        with open(clustered_mgf_file, 'rt') as mgf:
            #i = 0
            for line in mgf:
                if line[:6] == "TITLE=":
                    # Initiate new spectrum
                    #i += 1
                    peaklist = {
                        "m/z array": [],
                        "intensity array": [],
                    }
                    title = line[6:].strip()
                    peaklist["cluster_id"] = title.split(';')[0]
                    peaklist["spectrum_usi"] = title.split(';')[1]
                if line[:8] == "PEPMASS=":
                    peaklist["precursor mz"] = float(line[8:].strip())
                if line[:7] == "CHARGE=":
                    peaklist["precursor charge"] = int(line[7:].strip().strip("+"))
                if line[0].isdigit():
                    peak = line.strip().split(' ')
                    peaklist["m/z array"].append(float(peak[0]))
                    peaklist["intensity array"].append(float(peak[1]))
                if line.strip() == "END IONS":
                    # Finish up this spectrum
                    all_spectra.append(peaklist)
                    #if i > 100:
                    #    break

        # Group all spectra by cluster_id
        clusters = {}
        for peaklist in all_spectra:
            if peaklist['cluster_id'] not in clusters.keys():
                clusters[peaklist['cluster_id']] = [peaklist]
            else:
                clusters[peaklist['cluster_id']].append(peaklist)

        return clusters


    def combine_bin_mean(self, peaklists, minimum=100, maximum=2000, binsize=0.02, apply_peak_quorum=True):

        array_size = int( (maximum - minimum ) / binsize ) + 1
        merged_spectrum = { 'minimum': minimum, 'maximum': maximum, 'binsize': binsize }
        merged_spectrum['intensities'] = np.zeros(array_size, dtype=np.float32)
        merged_spectrum['mzs'] = np.zeros(array_size, dtype=np.float32)
        merged_spectrum['n_peaks'] = np.zeros(array_size, dtype=np.int32)
        merged_spectrum['precursor_mzs'] = []
        merged_spectrum['precursor_charges'] = []

        #### Determine how many peaks need to be present to keep a final peak
        peak_quorum = 1
        if apply_peak_quorum is True:
            peak_quorum = int(len(peaklists) * 0.25) + 1
            
        for peaklist in peaklists:
            #### Convert the peak lists to np arrays
            intensity_array = np.asarray(peaklist['intensity array'])
            mz_array = np.asarray(peaklist['m/z array'])

            #### Limit the np arrays to the region we're interested in
            intensity_array = intensity_array[ ( mz_array >= merged_spectrum['minimum'] ) & ( mz_array < merged_spectrum['maximum'] ) ]
            mz_array = mz_array[ ( mz_array >= merged_spectrum['minimum'] ) & ( mz_array < merged_spectrum['maximum'] ) ]

            #### Compute their bin locations and store n_peaks and intensities
            bin_array = (( mz_array - merged_spectrum['minimum'] ) / merged_spectrum['binsize']).astype(int)

            merged_spectrum['n_peaks'][bin_array] += 1
            merged_spectrum['intensities'][bin_array] += intensity_array
            merged_spectrum['mzs'][bin_array] += mz_array

            merged_spectrum['precursor_mzs'].append(peaklist['precursor mz'])
            merged_spectrum['precursor_charges'].append(peaklist['precursor charge'])

        # Check that all precursor charges are the same
        charges = merged_spectrum['precursor_charges']
        assert all(x == charges[0] for x in charges), "Not all precursor charges in cluster are equal"

        # Take the mean of all peaks per bin
        merged_spectrum['intensities'][merged_spectrum['n_peaks'] < peak_quorum] = np.nan
        merged_spectrum['intensities'] = np.divide(merged_spectrum['intensities'], merged_spectrum['n_peaks'])

        # Only return non-zero intensity bins
        nan_mask = ~np.isnan(merged_spectrum['intensities'])
        merged_spectrum['intensities'] = merged_spectrum['intensities'][nan_mask]

        #### EWD Changed this from just the bin size computation to taking the mean of mz values in the bin
        #merged_spectrum['mzs'] = np.arange(
        #    minimum + (binsize / 2), maximum + binsize, binsize, dtype=np.int32
        #)[nan_mask]
        merged_spectrum['mzs'][merged_spectrum['mzs'] == 0] = np.nan
        merged_spectrum['mzs'] = np.divide(merged_spectrum['mzs'], merged_spectrum['n_peaks'])
        merged_spectrum['mzs'] = merged_spectrum['mzs'][nan_mask]

        merged_spectrum['precursor_mz'] = np.mean(merged_spectrum['precursor_mzs'])
        merged_spectrum['precursor_charge'] = charges[0]

        del merged_spectrum['n_peaks']
        del merged_spectrum['precursor_charges']
        del merged_spectrum['precursor_mzs']

        return merged_spectrum


    def write_spectrum(self, spectra, mgf_file):
        for i, spectrum in enumerate(spectra):
            mgf_tmp = f"""BEGIN IONS
TITLE={spectrum["cluster_id"]}
PEPMASS={spectrum['precursor_mz']}
CHARGE={spectrum['precursor_charge']}+
"""
            for mz, intensity in zip(spectrum['mzs'], spectrum['intensities']):
                if not np.isnan(intensity):
                    mgf_tmp += f"{mz} {intensity}\n"
            mgf_tmp += 'END IONS\n\n'
            mgf_file.write(mgf_tmp)


####################################################################################################
#### For command-line usage
def main():

    argparser = argparse.ArgumentParser(description='Creates an index for an MSP spectral library file')
    argparser.add_argument('--verbose', action='count', help='If set, print more information about ongoing processing' )
    argparser.add_argument('--version', action='version', version='%(prog)s 0.5')
    #argparser.add_argument('--mara_file', action='store', help='Name of the mara clusters file')
    #argparser.add_argument('--mzml_file', action='store', help='Name of the mzml file')
    #argparser.add_argument('--cluster', action='store', help='Cluster number to combine')
    argparser.add_argument('--mgf_file', action='store', help='Name of the clustered MGF file')
    argparser.add_argument('--out', action='store', default='merged_spectra.mgf', help='Name of the output mgf file')
    params = argparser.parse_args()

    #### Set verbose
    verbose = params.verbose
    if verbose is None: verbose = 1

    #### Print and example if not everything is provided
    #if not params.mara_file or not params.mzml_file or not params.cluster:
    #    print("Example: representative_spectrum_creator.py --mzml_file ../data/01650b_BA5-TUM_first_pool_75_01_01-3xHCD-1h-R2.mzML --mara_file=../data/MaRaCluster.clusters_p30.tsv --cluster=1")
    #    print("Or use --help for additional usage information")
    #    sys.exit(10)

    if not params.mgf_file:
        print("Example: representative_spectrum_creator.py --mgf_file=../data/clustered_mgf.mgf")
        print("Or use --help for additional usage information")
        sys.exit(10)

    #### Create an Representative Spectrum Creator object
    rsc = RepresentativeSpectrumCreator(verbose=verbose)

    #### Read the cluster file from mzML and mara file
    #clusters = rsc.read_cluster_list(params.mara_file)
    #peaklists = rsc.read_spectra(params.mzml_file,clusters[int(params.cluster)])

    #### Read the cluster file from clustered MGF
    print("Reading spectra...")
    clusters = rsc.read_spectra_clustered_mgf(params.mgf_file)

    print("Clustering...")
    rsc_spectra = []
    #i = 0
    for cluster_id, peaklists in clusters.items():
        #i += 1
        #print(f"Cluster {cluster_id} contains {len(peaklists)} spectra")
        rsc_spectrum = rsc.combine_bin_mean(peaklists, minimum=100, maximum=2000, binsize=0.02)
        rsc_spectrum['cluster_id'] = cluster_id
        #print(f"Final spectrum has {len(rsc_spectrum['intensities'])} elements")
        rsc_spectra.append(rsc_spectrum)
        #if i > 20:
        #    break

    with open(params.out, 'wt') as mgf_file:
        rsc.write_spectrum(rsc_spectra, mgf_file)


#### For command line usage
if __name__ == "__main__": main()
