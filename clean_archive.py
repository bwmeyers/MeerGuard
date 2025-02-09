#!/usr/bin/env python

# For python3 and python2 compatibility
from __future__ import (absolute_import, division,
                        print_function, unicode_literals)

# Import CoastGuard
from coast_guard import cleaners
import argparse
import psrchive as ps
import os

def apply_surgical_cleaner(ar, tmp, cthresh=3.0, sthresh=3.0):
    print("Applying the surgical cleaner")
    print("\t channel threshold = {0}".format(cthresh))
    print("\t  subint threshold = {0}".format(sthresh))

    surgical_cleaner = cleaners.load_cleaner('surgical')
    surgical_parameters = "chan_numpieces=1,subint_numpieces=1,chanthresh={1},subintthresh={2},template={0}".format(tmp, cthresh, sthresh)
    surgical_cleaner.parse_config_string(surgical_parameters)
    surgical_cleaner.run(ar)

    

if __name__ == "__main__":
    # Parse some arguments to set up cleaning
    parser = argparse.ArgumentParser(description="Run MeerGuard on input archive file")
    parser.add_argument("-a", "--archive", type=str, dest="archive_path", help="Path to the archive file")
    parser.add_argument("-T", "--template", type=str, dest="template_path", help="Path to the 2D template file")
    parser.add_argument("-c", "--chanthresh", type=float, dest="chan_thresh", help="Channel threshold (in sigma) [default = 3.0]", default=3.0)
    parser.add_argument("-s", "--subthresh", type=float, dest="subint_thresh", help="Subint threshold (in sigma) [default = 3.0]", default=3.0)
    parser.add_argument("-o", "--outname", type=str, dest="output_name", help="Output archive name", default=None)
    parser.add_argument("-O", "--outpath", type=str, dest="output_path", help="Output path [default = CWD]", default=os.getcwd())
    args = parser.parse_args()


    # Load an Archive file
    loaded_archive = ps.Archive_load(args.archive_path)
    archive_path, archive_name = os.path.split(loaded_archive.get_filename())
    archive_name_pref = archive_name.split('.')[0]
    archive_name_suff = "".join(archive_name.split('.')[1:])
    #psrname = archive_name_orig.split('_')[0]

    # Renaming archive file with statistical thresholds
    if args.output_name is None:
        out_name = "{0}_ch{1}_sub{2}.ar".format(archive_name_pref, args.chan_thresh, args.subint_thresh, archive_name_suff)
    else:
        out_name = args.output_name


    apply_surgical_cleaner(loaded_archive, args.template_path, cthresh=args.chan_thresh, sthresh=args.subint_thresh)

    # Unload the Archive file
    print("Unloading the cleaned archive: {0}".format(out_name))
    loaded_archive.unload(str(out_name))  # need to typecast to str here because otherwise Python converts to a unicode string which the PSRCHIVE library can't parse

