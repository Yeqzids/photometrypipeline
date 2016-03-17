#!/usr/bin/env python

""" PP_EXTRACT - identify field sources using Source Extractor with
    multi-threading capabilities 
    v1.0: 2015-12-30, michael.mommert@nau.edu
"""

# Photometry Pipeline 
# Copyright (C) 2016  Michael Mommert, michael.mommert@nau.edu

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see
# <http://www.gnu.org/licenses/>.


import numpy
import os, sys
import subprocess
import logging
import argparse, shlex
import time, datetime
import Queue, threading
import logging
from astropy.io import fits

# pipeline-specific modules
import _pp_conf
from catalog import *
from toolbox import *

# setup logging
logging.basicConfig(filename = _pp_conf.log_filename, 
                    level    = _pp_conf.log_level,
                    format   = _pp_conf.log_formatline, 
                    datefmt  = _pp_conf.log_datefmt)

########## some definitions

version = '1.0'

# threading definitions
nThreads = 10
extractQueue = Queue.Queue(10000)
threadLock = threading.Lock()   


##### extractor class definition

class extractor(threading.Thread):
    """ 
    call Source Extractor using threading 
    """
    def __init__(self, par, output):
        self.param      = par
        self.output     = output
        threading.Thread.__init__(self)
    def run(self):
        while True:
            try:
                filename = extractQueue.get(True,1)
            except:
                break           # No more jobs in the queue
    
            # add output dictionary
            out = {}
            threadLock.acquire()
            self.output.append(out)
            threadLock.release()


            ### process this frame
            ldacname = filename[:filename.find('.fit')]+'.ldac' 
            out['fits_filename'] = filename
            out['ldac_filename'] = ldacname
            out['parameters']    = self.param


            # prepare running SEXTRACTOR
            threadLock.acquire()
            os.remove(ldacname) if os.path.exists(ldacname) else None
            threadLock.release()
            optionstring  = ' -PHOT_APERTURES %s ' % \
                            self.param['aperture_diam']
            optionstring += ' -BACKPHOTO_TYPE LOCAL '
            optionstring += ' -DETECT_MINAREA %f ' % \
                            self.param['source_minarea']
            optionstring += ' -DETECT_THRESH %f -ANALYSIS_THRESH %f ' % \
                        (self.param['sex_snr'], self.param['sex_snr'])
            optionstring += ' -CATALOG_NAME %s ' % ldacname

            if 'mask_file' in self.param:
                optionstring += ' -WEIGHT_TYPE MAP_WEIGHT'
                optionstring += ' -WEIGHT_IMAGE %s' %\
                                self.param['mask_file']

            if 'paramfile' in self.param:
                optionstring += ' -PARAMETERS_NAME %s' % \
                                self.param['paramfile']

            commandline = 'sex -c %s %s %s' % \
                          (self.param['obsparam']['sex-config-file'], 
                           optionstring, filename)
            
            # run SEXTRACTOR and wait for it to finish
            try:
                sex = subprocess.Popen(shlex.split(commandline), 
                                       stdout=subprocess.PIPE, 
                                       stderr=subprocess.PIPE, 
                                       universal_newlines=True)
            except:
                logging.error('cannot call Source Extractor')

            sex.wait()

            # read in LDAC file
            ldac_filename = filename[:filename.find('.fit')]+'.ldac'
            ldac_data = catalog(ldac_filename)
            ldac_data.read_ldac(ldac_filename, maxflag=None)
            out['catalog_data'] = ldac_data

            ### update image header with aperture radius and other information
            hdu = fits.open(filename, mode='update')
            obsparam = self.param['obsparam']
            # observation midtime
            if obsparam['obsmidtime_jd'] in hdu[0].header:
                midtimjd = hdu[0].header[obsparam['obsmidtime_jd']]
            else:
                if obsparam['date_keyword'].find('|') == -1:
                    midtimjd = dateobs_to_jd(\
                        hdu[0].header[obsparam['date_keyword']]) + \
                        float(hdu[0].header[obsparam['exptime']])/2./86400.
                else:
                    datetime = hdu[0].header[\
                                    obsparam['date_keyword'].split('|')[0]]+ \
                        'T'+hdu[0].header[\
                                    obsparam['date_keyword'].split('|')[1]]
                    midtimjd = dateobs_to_jd(datetime) + \
                               float(hdu[0].header[\
                                            obsparam['exptime']])/2./86400.
            out['time'] = midtimjd

            hdu[0].header['APRAD'] = \
                (",".join([str(aprad) for aprad in self.param['aprad']]), \
                 'aperture phot radius (px)')
            hdu[0].header['SEXSNR'] = \
                (self.param['sex_snr'], 
                 'Sextractor detection SNR threshold')
            hdu[0].header['SEXAREA'] = \
                (self.param['source_minarea'], 
                 'Sextractor source area threshold (px)')
            out['fits_header'] = hdu[0].header

            threadLock.acquire()
            hdu.flush()
            threadLock.release()

            hdu.close()
            
            threadLock.acquire()   
            logging.info("%d sources extracted from frame %s" % \
                         (len(ldac_data.data), filename))
            if not self.param['quiet']:
                print "%d sources extracted from frame %s" % \
                    (len(ldac_data.data), filename)
            threadLock.release()
            
            extractQueue.task_done()  # inform queue that this task is done
                

def extract_multiframe(filenames, parameters):
    """
    wrapper to run multi-threaded source extraction
    input: FITS filenames, parameters dictionary: telescope, obsparam, aprad,
                                                  quiet, sex_snr, source_minarea
    output: result properties
    """
    
    logging.info('extract sources from %d files using Source Extractor' % \
                 len(filenames))
    logging.info('extraction parameters: %s' % repr(parameters))


    # obtain telescope information from image header or override manually
    hdu = fits.open(filenames[0])

    if 'telescope' not in parameters or parameters['telescope'] is None: 
        try:
            parameters['telescope'] = hdu[0].header['TEL_KEYW']
        except KeyError:
            logging.critical('ERROR: TEL_KEYW not in image header (%s)' %
                             filenames[0])
            print 'ERROR: TEL_KEYW not in image header;' + \
                  'has this image run through register?'
            return {}
    try:
        parameters['obsparam'] = _pp_conf.telescope_parameters[\
                                                parameters['telescope']]
    except KeyError:
        print "ERROR: telescope '%s' is unknown." % telescope
        logging.critical('ERROR: telescope \'%s\' is unknown.' % telescope)
        return {}

    # set aperture photometry DIAMETER as string
    if ((type(parameters['aprad']) == float and parameters['aprad'] == 0)
        or (type(parameters['aprad']) == list
            and len(parameters['aprad']) == 0)):
        parameters['aperture_diam'] = str(parameters['obsparam']
                                          ['aprad_default']*2)
    else:
        if not isinstance(parameters['aprad'], list) and \
           not isinstance(parameters['aprad'], numpy.ndarray):
            parameters['aprad'] = [str(parameters['aprad'])]
        parameters['aperture_diam'] = ','.join([str(float(rad)*2.) for 
                                                rad in parameters['aprad']])


    #check what the binning is and if there is a mask available
    if '_' in parameters['obsparam']['binning'][0]:
        if '_blank' in parameters['obsparam']['binning'][0]:
            binning_x = float(hdu[0].header[\
                            parameters['obsparam']['binning'][0].\
                                     split('_')[0]].split()[0])
            binning_y = float(hdu[0].header[\
                            parameters['obsparam']['binning'][1].\
                                     split('_')[0]].split()[1])
    else:
        binning_x = hdu[0].header[parameters['obsparam']['binning'][0]]
        binning_y = hdu[0].header[parameters['obsparam']['binning'][1]]
    bin_string = '%d,%d' % (binning_x, binning_y)

    if bin_string in parameters['obsparam']['mask_file']:
        mask_file = parameters['obsparam']['mask_file'][bin_string]
        parameters['mask_file'] = mask_file


    ### thread and queue handling

    output = []

    # populate the queue with frame filenames
    for filename in filenames:
        while True:
            if extractQueue.full():
                time.sleep(0.5)
            else:
                break
        extractQueue.put(filename, block=True)

    # spawning threads
    # never spawn more threads than there are items in the queue!
    for thread in range(min([nThreads, len(filenames)])):
        extractor(parameters, output).start()

    # waiting for threads to finish
    threadLock.acquire()
    threadLock.release()
    extractQueue.join()


    ### output content
    #
    # { 'fits_filename': fits filename,
    #   'ldac_filename': LDAC filename,
    #   'parameters'   : source extractor input parameters,
    #   'catalog_data' : full LDAC catalog data,
    #   'time'         : observation midtime (JD),
    #   'fits_header'  : complete fits header
    # }
    ###

    return output


############ MAIN

if __name__ == '__main__':

    # define command line arguments                                             
    parser = argparse.ArgumentParser(description='source detection and' + \
                                     'photometry using Source Extractor')
    parser.add_argument("-snr", help='sextractor SNR threshold', default=1.5)
    parser.add_argument("-minarea", help='sextractor source area threshold',
                        default=3)
    parser.add_argument("-paramfile",
                        help='alternative sextractor parameter file',
                        default=None)
    parser.add_argument("-aprad",
                        help='aperture radius (list) for photometry (px)',
                        default='')
    parser.add_argument("-telescope", help='manual telescope override',
                        default=None)
    parser.add_argument('-quiet', help='no logging',
                        action="store_true")
    parser.add_argument('images', help='images to process', nargs='+')
    
    args = parser.parse_args()
    sex_snr = float(args.snr)
    source_minarea = float(args.minarea)
    paramfile = args.paramfile
    aprad = args.aprad
    telescope = args.telescope
    quiet = args.quiet
    filenames = args.images 

    # prepare parameter dictionary
    parameters = {'sex_snr':sex_snr, 'source_minarea':source_minarea, \
                  'aprad':aprad, 'telescope':telescope, 'quiet':quiet}

    if paramfile is not None:
        parameters['paramfile'] = paramfile
    
    ### call extraction wrapper
    extraction = extract_multiframe(filenames, parameters)
