#!/usr/bin/env python

import multiprocessing
import subprocess
import warnings
import tempfile
import datetime
import fnmatch
import os.path
import shutil
import glob
import sys
import os

import config
import utils
import diagnose
import cleaners
import combine
import database
import clean_utils
import errors

import pyriseset as rs

SAVE_INTERMEDIATE = True

EFF = rs.sites.load('effelsberg')

# Observing log fields:
#                  name   from-string converter
OBSLOG_FIELDS = (('localdate', rs.utils.parse_datestr), \
                 ('scannum', str), \
                 ('utcstart', rs.utils.parse_timestr), \
                 ('lststart', rs.utils.parse_timestr), \
                 ('name', str), \
                 ('az', float), \
                 ('alt', float), \
                 ('catalog_rastr', str), \
                 ('catalog_decstr', str))


RCVR_INFO = {'P217-3': 'rcvr:name=P217-3,rcvr:hand=-1,rcvr:basis=cir', \
             'S110-1': 'rcvr:name=S110-1,rcvr:hand=-1,rcvr:basis=cir', \
             'P200-3': 'rcvr:name=P200-3,rcvr:hand=-1,rcvr:basis=cir'}


def load_directories(db, *args, **kwargs):
    """Search for directories containing asterix data.
        For each newly found entry, insert a row in the
        database.

        Input:
            db: Database object to use.
            ** Additional arguments are passed on to 'get_rawdata_dirs' **

        Output:
            ninserts: Number of new directories inserted.
    """
    ninserts = 0
    dirs = get_rawdata_dirs(*args, **kwargs)
    nn = len(dirs)
    for ii, path in utils.show_progress(enumerate(dirs), tot=nn, width=50):
        try:
            with db.transaction() as conn:
                insert = db.directories.insert().\
                        values(path=path)
                # 'directories.path' is constrained to be unique, so
                # trying to insert a directory that already exists
                # will result in an error, which will be automatically
                # rolled back by the context manager (i.e. no new
                # database entry will be inserted)
                conn.execute(insert)
        except:
            pass
        else:
            # The following line is only reached if the execution
            # above doesn't raise an exception
            ninserts += 1
    return ninserts


def load_groups(db, dirrow):
    """Given a row from the DB's directories table create a group 
        listing from the asterix data stored in the directories 
        and load it into the database.

        Inputs:
            db: Database object to use.
            dirrow: A row from the directories table.

        Outputs:
            ninserts: The number of group rows inserted.
    """
    path = dirrow['path']
    dir_id = dirrow['dir_id']
    if dirrow['status'] != 'new':
        return errors.BadStatusError("Groupings can only be " \
                                "generated for 'directory' entries " \
                                "with status 'new'. (The status of " \
                                "Dir ID %d is '%s'.)" % \
                                (dir_id, dirrow['status']))
    try:
        ninserts = 0
        values = []
        for dirs, fns, band in zip(*make_groups(path)):
            fns.sort()
            listfn = os.path.join(config.output_location, 'groups', \
                                    "%s_%s_%dsubints.txt" % \
                                    (fns[0], band, len(fns)))
            combine.write_listing(dirs, fns, listfn)
            listpath, listname = os.path.split(listfn)
            values.append({'listpath': listpath, \
                           'listname': listname, \
                           'md5sum': utils.get_md5sum(listfn)})
    except:
        with db.transaction() as conn:
            update = db.directories.update().\
                        where(db.directories.c.dir_id==dir_id).\
                        values(status='failed')
            conn.execute(update)
        raise
    else:
        with db.transaction() as conn:
            version_id = utils.get_version_id(db) 
            insert = db.groupings.insert().\
                        values(version_id = version_id, \
                               dir_id = dir_id)
            conn.execute(insert, values)
            update = db.directories.update().\
                        where(db.directories.c.dir_id==dir_id).\
                        values(status='grouped', \
                                last_modified=datetime.datetime.now())
            conn.execute(update)
        ninserts += len(values)
    return ninserts


def load_combined_file(db, grprow):
    """Given a row from the DB's groups table create a combined
        archive and load it into the database.

        Input:
            db: Database object to use.
            grprow: A row from the groupings table.

        Outputs:
            file_id: The ID of newly loaded 'combined' file.
    """
    group_id = grprow['group_id']
    if grprow['status'] != 'new':
        return errors.BadStatusError("Combined files can only be " \
                                "generated from 'grouping' entries " \
                                "with status 'new'. (The status of " \
                                "Group ID %d is '%s'.)" % \
                                (group_id, grprow['status']))
    listfn = os.path.join(grprow['listpath'], grprow['listname'])
    try:
        subdirs, subints = combine.read_listing(listfn)
        # Combine the now-prepped subints
        cmbdir = os.path.join(config.output_location, 'combined')
        cmbfn = make_combined_file(subdirs, subints, outdir=cmbdir)
 
        # Pre-compute values to insert because some might be
        # slow to generate
        arf = utils.ArchiveFile(cmbfn)
        if arf['name'].endswith("_R"):
            obstype = 'cal'
        else:
            obstype = 'pulsar'
 
        values = {'filepath': cmbdir, \
                  'filename': os.path.basename(cmbfn), \
                  'sourcename': arf['name'], \
                  'obstype': obstype, \
                  'stage': 'combined', \
                  'md5sum': utils.get_md5sum(cmbfn), \
                  'filesize': os.path.getsize(cmbfn)}
    except:
        with db.transaction() as conn:
            update = db.groupings.update(). \
                        where(db.groupings.c.group_id==group_id).\
                        values(status='failed', \
                                last_modified=datetime.datetime.now())
            conn.execute(update)
        raise
    else:
        with db.transaction() as conn:
            version_id = utils.get_version_id(db)
            # Insert new entry
            insert = db.files.insert().\
                    values(version_id = version_id, \
                            group_id = group_id)
            result = conn.execute(insert, values)
            file_id = result.inserted_primary_key[0]
            # Update status of groupings
            update = db.groupings.update(). \
                        where(db.groupings.c.group_id==group_id).\
                        values(status='combined', \
                                last_modified=datetime.datetime.now())
            conn.execute(update)
    return file_id


def load_corrected_file(db, filerow):
    """Given a row from the DB's files table referring to a
        status='new', stage='combined' file, process the file
        by correcting its header and load the new file into
        the database.

        Inputs:
            db: Database object to use.
            filerow: A row from the files table.

        Output:
            file_id: The ID of the newly loaded 'corrected' file.
    """
    parent_file_id = filerow['file_id']
    if (filerow['status'] != 'new') or (filerow['stage'] != 'combined'):
        return errors.BadStatusError("Corrected files can only be " \
                        "generated from 'file' entries with " \
                        "status='new' and stage='combined'. " \
                        "(For File ID %d: status='%s', stage='%s'.)" % \
                        (parent_file_id, filerow['status'], filerow['stage']))
    infn = os.path.join(filerow['filepath'], filerow['filename'])
    try:
        corrfn, corrstr = correct_header(infn)

        arf = utils.ArchiveFile(corrfn)

        # Move file to archive directory
        archivedir = os.path.join(config.output_location, \
                                config.output_layout) % arf
        archivefn = (config.outfn_template+".corr") % arf
        if not os.path.exists(archivedir):
            os.makedirs(archivedir)
        shutil.move(corrfn, os.path.join(archivedir, archivefn))
        # Update 'corrfn' so it still refers to the file
        corrfn = os.path.join(archivedir, archivefn)

        # Pre-compute values to insert because some might be
        # slow to generate
        arf = utils.ArchiveFile(corrfn)
        values = {'filepath': archivedir, \
                  'filename': archivefn, \
                  'sourcename': filerow['sourcename'], \
                  'obstype': filerow['obstype'], \
                  'stage': 'corrected', \
                  'md5sum': utils.get_md5sum(corrfn), \
                  'filesize': os.path.getsize(corrfn), \
                  'parent_file_id': parent_file_id}
    except:
        with db.transaction() as conn:
            update = db.files.update(). \
                        where(db.files.c.file_id==parent_file_id).\
                        values(status='failed', \
                                last_modified=datetime.datetime.now())
            conn.execute(update)
        raise
    else:
        with db.transaction() as conn:
            version_id = utils.get_version_id(db)
            # Insert new entry
            insert = db.files.insert().\
                    values(version_id = version_id, \
                            group_id = filerow['group_id'])
            result = conn.execute(insert, values)
            file_id = result.inserted_primary_key[0]
            # Update parent file
            update = db.files.update(). \
                        where(db.files.c.file_id==parent_file_id).\
                        values(status='processed', \
                                last_modified=datetime.datetime.now())
            conn.execute(update)
        move_file(db, parent_file_id, archivedir, 
                    (config.outfn_template+".cmb") % arf)
        move_grouping(db, parent_file_id, archivedir, 
                    (config.outfn_template+".list.txt") % arf)
    return file_id


def load_cleaned_file(db, filerow):
    """Given a row from the DB's files table referring to a
        status='new', stage='combined' file, process the file
        by cleaning it and load the new file into the database.

        Inputs:
            db: Database object to use.
            filerow: A row from the files table.

        Ouput:
            file_id: The ID of the newly loaded 'cleaned' file.
    """
    parent_file_id = filerow['file_id']
    if (filerow['status'] != 'new') or (filerow['stage'] != 'corrected'):
        return errors.BadStatusError("Cleaned files can only be " \
                        "generated from 'file' entries with " \
                        "status='new' and stage='corrected'. " \
                        "(For File ID %d: status='%s', stage='%s'.)" % \
                        (parent_file_id, filerow['status'], filerow['stage']))
    infn = os.path.join(filerow['filepath'], filerow['filename'])
    try:
        arf = utils.ArchiveFile(infn)
        # Clean the data file
        config.cfg.load_configs_for_archive(arf)
        cleaner_queue = [cleaners.load_cleaner('rcvrstd'), \
                         cleaners.load_cleaner('surgical')]
        for cleaner in cleaner_queue:
            cleaner.run(arf.get_archive())

        # Write out the cleaned data file
        archivedir = os.path.join(config.output_location, \
                                config.output_layout) % arf
        archivefn = (config.outfn_template+".clean") % arf
        cleanfn = os.path.join(archivedir, archivefn)

        # Make sure output directory exists
        if not os.path.exists(archivedir):
            os.makedirs(archivedir)

        arf.get_archive().unload(cleanfn)
       
        # Pre-compute values to insert because some might be
        # slow to generate
        values = {'filepath': archivedir, \
                  'filename': archivefn, \
                  'sourcename': filerow['sourcename'], \
                  'obstype': filerow['obstype'], \
                  'stage': 'cleaned', \
                  'md5sum': utils.get_md5sum(cleanfn), \
                  'filesize': os.path.getsize(cleanfn), \
                  'parent_file_id': parent_file_id}
    except:
        with db.transaction() as conn:
            update = db.files.update(). \
                        where(db.files.c.file_id==parent_file_id).\
                        values(status='failed', \
                                last_modified=datetime.datetime.now())
            conn.execute(update)
        raise
    else:
        with db.transaction() as conn:
            version_id = utils.get_version_id(db)
            # Insert new entry
            insert = db.files.insert().\
                    values(version_id = version_id, \
                            group_id = filerow['group_id'])
            result = conn.execute(insert, values)
            file_id = result.inserted_primary_key[0]
            # Update parent file
            update = db.files.update(). \
                        where(db.files.c.file_id==parent_file_id).\
                        values(status='processed', \
                                last_modified=datetime.datetime.now())
            conn.execute(update)
    return file_id


def move_grouping(db, group_id, destdir, destfn=None):
    """Given a group ID move the associated listing.

        Inputs:
            db: Database object to use.
            group_id: The ID of a row in the groupings table.
            destdir: The destination directory.
            destfn: The destination file name.
                (Default: Keep old file name).

        Outputs:
            None
    """
    with db.transaction() as conn:
        select = db.select([db.groupings]).\
                    where(db.groupings.c.group_id==group_id)
        result = conn.execute(select)
        rows = result.fetchall()
        if len(rows) != 1:
            raise errors.DatabaseError("Bad number of rows (%d) " \
                                "with group_id=%d!" % \
                                (len(rows), group_id))
        grp = rows[0]
        if destfn is None:
            destfn = grp['listname']
        # Copy file
        src = os.path.join(grp['listpath'], grp['listname'])
        dest = os.path.join(destdir, destfn)
        if not os.path.exists(destdir):
            os.makedirs(destdir)
        shutil.copy(src, dest)
        # Update database
        update = db.groupings.update().\
                    where(db.groupings.c.group_id==group_id).\
                    values(listpath=destdir, \
                            listname=destfn, \
                            last_modified=datetime.datetime.now())
        conn.execute(update)
        # Remove original
        os.remove(src)
        utils.print_info("Moved group listing from %s to %s. The database " \
                        "has been updated accordingly." % (src, dest))

def move_file(db, file_id, destdir, destfn=None):
    """Given a file ID move the associated archive.

        Inputs:
            db: Database object to use.
            file_id: The ID of a row in the files table.
            destdir: The destination directory.
            destfn: The destination file name.
                (Default: Keep old file name).

        Outputs:
            None
    """
    with db.transaction() as conn:
        select = db.select([db.files]).\
                    where(db.files.c.file_id==file_id)
        result = conn.execute(select)
        rows = result.fetchall()
        if len(rows) != 1:
            raise errors.DatabaseError("Bad number of rows (%d) " \
                                "with file_id=%d!" % \
                                (len(rows), file_id))
        ff = rows[0]
        if destfn is None:
            destfn = ff['filename']
        # Copy file
        src = os.path.join(ff['filepath'], ff['filename'])
        dest = os.path.join(destdir, destfn)
        if not os.path.exists(destdir):
            os.makedirs(destdir)
        shutil.copy(src, dest)
        # Update database
        update = db.files.update().\
                    where(db.files.c.file_id==file_id).\
                    values(filepath=destdir, \
                            filename=destfn, \
                            last_modified=datetime.datetime.now())
        conn.execute(update)
        # Remove original
        os.remove(src)
        utils.print_info("Moved archive file from %s to %s. The database " \
                        "has been updated accordingly." % (src, dest))


def get_rawdata_dirs(basedir=None):
    """Get a list of directories likely to contain asterix data.
        Directories 2 levels deep with a name "YYYYMMDD" are returned.

        Input:
            basedir: Root of the directory tree to search.

        Output:
            outdirs: List of likely raw data directories.
    """
    if basedir is None:
        basedir = config.base_rawdata_dir
    outdirs = []
    indirs = glob.glob(os.path.join(basedir, '*'))
    for path in indirs:
        subdirs = glob.glob(os.path.join(path, "*"))
        for subdir in subdirs:
            if os.path.isdir(subdir):
                try:
                    datetime.datetime.strptime(os.path.basename(subdir), "%Y%m%d")
                except:
                    pass
                else:
                    # Is a directory whose name has the required format
                    outdirs.append(subdir)
    return outdirs


def make_groups(path):
    """Given a directory containing asterix subint files
        return a list of subint groups.

        Input:
            path: A directory containing frequency sub-band 
                directories.

        Output:
            usedirs_list: List of lists of directories to use when combining.
                (NOTE: This may be different than the input
                    'subdirs' because some directories may have
                    too few subints to be worth combining. This
                    depends on the input value of 'tossfrac'.)
            groups_list: List of lists of groups of files to be combined.
                (NOTE: These are the file name only (i.e. no path)
                    Each file listed appears in each of 'usedirs'.)
            band_list: List of band names.
    """
    usedirs_list = []
    groups_list = []
    band_list = []

    # Try L-band and S-band
    for band, subdir_pattern in \
                    zip(['Lband', 'Sband'], ['1'+'[0-9]'*3, '2'+'[0-9]'*3]):
        subdirs = glob.glob(os.path.join(path, subdir_pattern))
        if subdirs:
            utils.print_info("Found %d freq sub-band dirs for %s in %s. " \
                        "Will group sub-ints contained" % \
                        (len(subdirs), band, path), 2)
            usedirs, groups = combine.group_subband_dirs(subdirs)
            # Keep track of the groups and directories used
            for grp in groups:    
                band_list.append(band)
                groups_list.append(grp)
                usedirs_list.append(usedirs)
    return usedirs_list, groups_list, band_list


def make_combined_file(subdirs, subints, outdir):
    """Given lists of directories and subints combine them.

        Inputs:
            subdirs: List of sub-band directories containing 
                sub-ints to combine
            subints: List of subint files to be combined.
                (NOTE: These are the file name only (i.e. no path)
                    Each file listed should appear in each of the
                    subdirs.)
            outdir: Directory to copy combined file to.

        Outputs:
            outfn: The name of the combined archive.
    """
    # Work in a temporary directory
    tmpdir = tempfile.mkdtemp(suffix="_combine", \
                                    dir=config.tmp_directory)
    try:
        # Prepare subints
        preppeddirs = prepare_subints(subdirs, subints, \
                            baseoutdir=os.path.join(tmpdir, 'data'))
        cmbfn = combine.combine_subints(preppeddirs, subints, outdir=outdir)
    except:
        raise # Re-raise the exception
    finally:
        #warnings.warn("Not cleaning up temporary directory (%s)" % tmpdir)
        utils.print_info("Removing temporary directory (%s)" % tmpdir, 2)
        shutil.rmtree(tmpdir)
    return cmbfn


def prepare_subints(subdirs, subints, baseoutdir):
    """Prepare subints by
           - Moving them to the temporary working directory
           - De-weighting 6.25% from each sub-band edge
           - Converting archive format to PSRFITS

        Inputs:
            subdirs: List of sub-band directories containing 
                sub-ints to combine
            subints: List of subint files to be combined.
                (NOTE: These are the file name only (i.e. no path)
                    Each file listed should appear in each of the
                    subdirs.)
            baseoutdir: Directory containing the sub-directories
                of preprared files.

        Outputs:
            prepsubdirs: The sub-directories containing prepared files.
    """
    devnull = open(os.devnull)
    tmpsubdirs = []
    print "Preparing subints..."
    for subdir in utils.show_progress(subdirs, width=50):
        freqdir = os.path.split(os.path.abspath(subdir))[-1]
        freqdir = os.path.join(baseoutdir, freqdir)
        if not os.path.exists(freqdir):
            os.makedirs(freqdir)
        fns = [os.path.join(subdir, fn) for fn in subints]
        utils.execute(['paz', '-j', 'convert psrfits', \
                            '-E', '6.25', '-O', freqdir] + fns, \
                        stderr=devnull)
        tmpsubdirs.append(freqdir)
    utils.print_info("Prepared %d subint fragments in %d freq sub-dirs" % \
                    (len(subints), len(subdirs)), 3)
    return tmpsubdirs


def correct_header(arfn):
    """Correct header of asterix data in place.

        Input:
            arfn: The name of the input archive file.

        Output:
            corrfn: The name of the corrected file.
            corrstr: The parameter string of corrections used with psredit.
    """
    # Load archive
    arf = utils.ArchiveFile(arfn)
    if arf['rcvr'].upper() in RCVR_INFO:
        rcvr = arf['rcvr']
    elif arf['freq'] > 2000: 
        # S-band
        rcvr = 'S110-1'
    else:
        ar = arf.get_archive()
        nchan = ar.get_nchan()
        # Scrunch
        ar.pscrunch()
        ar.tscrunch()
        # Get the relevant data
        chnwts = clean_utils.get_chan_weights(ar).astype(bool)
        stddevs = ar.get_data().squeeze().std(axis=1)
        bot = stddevs[:nchan/8][chnwts[:nchan/8]].mean()
        top = stddevs[nchan/8:][chnwts[nchan/8:]].mean()
        if top/bot > 5:
            # L-band receiver
            rcvr = 'P200-3'
        elif top/bot < 2:
            # 7-beam receiver
            rcvr = 'P217-3'
        else:
            raise utils.HeaderCorrectionError("Cannot determine receiver.")
    corrstr = "%s,be:name=asterix" % RCVR_INFO[rcvr]
    if arf['name'].endswith("_R"):
        corrstr += ",type=PolnCal"
    else:
        corrstr += ",type=Pulsar"
    if arf['name'].endswith('_R') or arf['ra'].startswith('00:00:00'):
        # Correct coordinates
        obsinfo = get_obslog_entry(arf)
        ra_deg, decl_deg = EFF.get_skyposn(obsinfo['alt'], obsinfo['az'], \
                                            lst=obsinfo['lststart'])
        rastr = rs.utils.deg_to_hmsstr(ra_deg, decpnts=3)[0]
        decstr = rs.utils.deg_to_dmsstr(decl_deg, decpnts=2)[0]
        if decstr[0] not in ('-', '+'):
            decstr = "+" + decstr
        corrstr += ",coord=%s%s" % (rastr, decstr)
    # Correct the file using 'psredit'
    utils.execute(['psredit', '-e', 'corr', '-c', corrstr, arfn])
    # Assume the name of the corrected file
    corrfn = os.path.splitext(arfn)[0]+".corr"
    # Confirm that our assumed file name is accurate
    if not os.path.isfile(corrfn):
        raise errors.HeaderCorrectionError("The corrected file (%s) does not " \
                                "exist!" % corrfn)
    return corrfn, corrstr


def get_obslog_entry(arf):
    """Given an archive file, find the entry in the observing log.

        Input:
            arf: ArchiveFile object.

        Output:
            obsinfo: A dictionary of observing information.
    """
    # Get date of observation
    obsdate = rs.utils.mjd_to_datetime(arf['mjd'])
    obsutc = obsdate.time()
    obsutc_hours = obsutc.hour+(obsutc.minute+(obsutc.second)/60.0)/60.0

    # Get log file
    # NOTE: Date in file name is when the obslog was written out
    obslogfns = glob.glob(os.path.join(config.obslog_dir, "*.prot"))
    obslogfns.sort()
    for currfn in obslogfns:
        currdate = datetime.datetime.strptime(os.path.split(currfn)[-1], \
                                            '%y%m%d.prot')
        obslogfn = currfn
        if currdate > obsdate:
            break
    if obslogfn is None:
        raise errors.HeaderCorrectionError("Could not find a obslog file " \
                                    "from before the obs date (%s)." % \
                                    obsdate.strftime("%Y-%b-%d"))

    with open(obslogfn, 'r') as obslog:
        logentries = []
        bestoffset = 1e10
        for line in obslog:
            valstrs = line.split()
            if len(valstrs) < len(OBSLOG_FIELDS):
                continue
            currinfo = {}
            for (key, caster), valstr in zip(OBSLOG_FIELDS, valstrs):
                currinfo[key] = caster(valstr)
            if utils.get_prefname(currinfo['name']) != arf['name']:
                continue
            utc_hours = currinfo['utcstart'][0]
            offset = obsutc_hours - utc_hours
            if offset*3600 < 120:
                logentries.append(currinfo)
        if len(logentries) != 1:
            raise errors.HeaderCorrectionError("Bad number (%d) of entries " \
                                "in obslog (%s) with correct source name " \
                                "within 120 s of observation (%s) start " \
                                "time (UTC: %s)" % \
                                (len(logentries), obslogfn, arf.fn, obsutc))
        return logentries[0]


def make_summary_plots(arf):
    """Make two summary plots. One with the native time/freq/bin resolution
        and nother that is partially scrunched.

        Input:
            arf: An ArchiveFile object.

        Outputs:
            fullresfn: The name of the high-resolution summary plot file.
            lowresfn: The name of the low-resolution summary plot file.
    """
    fullresfn = arf.fn+".png"
    diagnose.make_composite_summary_plot(arf, outfn=fullresfn)
    
    preproc = 'C,D,B 128,F 32'
    if arf['nsub'] > 32:
        preproc += ",T 32"
    lowresfn = arf.fn+".scrunched.png"
    diagnose.make_composite_summary_plot(arf, preproc, outfn=lowresfn)
 
    return fullresfn, lowresfn


def reduce_directory(path):
    # Create temporary working directory
    basetmpdir = tempfile.mkdtemp(suffix="_reduce", \
                                    dir=config.tmp_directory)
    utils.print_info("Reducing data in %s. Temporary working directory: %s" % \
                (path, basetmpdir), 2)
    try:
        tmpdir = os.path.join(basetmpdir, 'work')
        toignore = lambda visitdir, xx: [x for x in xx \
                        if (os.path.isfile(x) and not x.endswith('.ar'))]
        # Copy *.ar files to working directory
        shutil.copytree(path, tmpdir, ignore=toignore)
        utils.execute(['chmod', '-R', '700', tmpdir])
        utils.print_info("Copied data to working directory.", 3)
        # Change to working directory
        os.chdir(tmpdir)
        # Prepare copied files
        freqdirs = {}
        nfragments = 0
        for (dirpath, dirnames, filenames) in os.walk(tmpdir):
            utils.print_debug("Walking through data directory %s. " \
                            "Found %d directories and %d files." % \
                            (dirpath, len(dirnames), len(filenames)), 'reduce')
            if fnmatch.fnmatch(os.path.split(dirpath)[-1], '[12]???'):
                # Directory is a frequency sub-band
                dirs = freqdirs.setdefault(os.path.split(dirpath)[-1][0], [])
                dirs.append(dirpath)
                arfns = [os.path.join(dirpath, xx) for xx in filenames \
                                if xx.endswith('.ar')]
                nfragments += len(arfns)
                if arfns:
                    # Convert files to PSRFITS format
                    utils.execute(['psrconv', '-m', '-o', 'PSRFITS'] + arfns) 
                    # Remove sub-band edges
                    utils.execute(['paz', '-E', '6.25', '-m'] + arfns)
        utils.print_info("Prepared %d subint fragments in %d freq sub-dirs" % \
                        (nfragments, len(freqdirs)), 3)
        for dirs in freqdirs.values():
            # Create a sub-directory
            subdir = tempfile.mkdtemp(suffix="_subdir", dir=tmpdir)
            os.chdir(subdir)
            # Create directory for combined files
            os.mkdir('combined_files')
            
            # Combine sub-bands for each sub-int independently
            utils.execute(['combine_ff.sh'] + dirs, stderr=open(os.devnull, 'w'))

            cmbsubints = glob.glob(os.path.join(subdir, 'combined_*.ar'))
            # Join combined sub-ints together
            utils.execute(['psradd', '-O', 'combined_files', '-autoT', '-g', \
                            '3600', '-G', '119'] + cmbsubints)
            cmbfns = glob.glob(os.path.join(subdir, 'combined_files', 'combined_*'))
            utils.print_info("Combined subints into %d files" % len(cmbfns), 2)
            for tmp in cmbfns:
                to_save = [] # List of files to copy to results directory
                # Create ArchiveFile object
                arf = utils.ArchiveFile(tmp)
                # Set configurations
                config.cfg.load_configs_for_archive(arf)
                
                # Adjust header in preparation for calibration
                if arf['name'].endswith("_R"):
                    # Is a calibration scan
                    utils.execute(['psredit', '-m', '-c', 'rcvr:hand=-1,rcvr:basis=cir,type=PolnCal', tmp])
                    cleanext = ".pcal"
                else:
                    utils.execute(['psredit', '-m', '-c', 'rcvr:hand=-1,rcvr:basis=cir,type=Pulsar', tmp])
                    cleanext = ".ar"
                arf = utils.correct_asterix_header(arf)
                # Reload configuration because header has changed
                config.cfg.load_configs_for_archive(arf)
                
                # Base name (ie no extension) of output file
                baseoutfn = config.outfn_template % arf
                cleanfn = baseoutfn+cleanext
 
                # Rename combined file
                cmbfn = baseoutfn+".cmb"
                os.rename(arf.fn, cmbfn)
                arf.fn = cmbfn
                arf.get_archive().set_filename(cmbfn)
 
                # Make pre-cleaning diagnostic plots
                to_save.extend(make_summary_plots(arf))
 
                # Clean the data
                utils.print_info("Cleaning %s" % arf.fn, 1)
                # Load cleaners here because each data file might
                # have different configurations. The configurations
                # are set when the cleaner is loaded.
                cleaner_queue = [cleaners.load_cleaner('rcvrstd'), \
                                 cleaners.load_cleaner('surgical')]
 
                for cleaner in cleaner_queue:
                    cleaner.run(arf.get_archive())
                arf.get_archive().unload(cleanfn)
                to_save.append(cleanfn)
                
                cleanarf = utils.ArchiveFile(cleanfn)
                
                # Make post-cleaning diagnostic plots
                to_save.extend(make_summary_plots(cleanarf))
 
                if cleanarf['name'].endswith("_R"):
                    # This is a calibration scan

                    # Reduce number of channels to 16 per subband
                    # We use the number of subbands because occasionally some
                    # are missing, meaning we don't expect the full 128 channels
                    nchans = 16*len(dirs)
                    utils.execute(['pam', '--setnchn', '%d' % nchans, '-T', '-e', 'pcal.T', cleanfn])
                    to_save.append(cleanfn+'.T')
                else:
                    if SAVE_INTERMEDIATE:
                        # Copy combined file (before cleaning) to output directory
                        to_save.append(cmbfn)
                
                # Copy results files
                outdir = os.path.join(config.output_location, \
                                config.output_layout) % cleanarf 
                # Create output directory, if necessary
                if not os.path.exists(outdir):
                    os.makedirs(outdir)
                for fn in to_save:
                    shutil.copy(fn, os.path.join(outdir, os.path.split(fn)[-1]))
    finally:
        #warnings.warn("Not cleaning up temporary directory (%s)" % tmpdir)
        #utils.print_info("Removing temporary directory (%s)" % tmpdir, 1)
        shutil.rmtree(tmpdir)


def main():
    if args.numproc > 1:
        pool = multiprocessing.Pool(processes=args.numproc)
        results = []
        paths = []
        for path in args.path:
            paths.append(path)
            results.append(pool.apply_async(reduce_directory, args=(path,)))
        pool.close()
        pool.join()
 
        # Check results
        for path, result in zip(paths, results):
            result.get()
    else:
        for path in args.path:
            reduce_directory(path)


if __name__ == '__main__':
    parser = utils.DefaultArguments(description="Automated reduction " \
                                    "of Asterix data.")
    parser.add_argument("path", nargs='+', type=str,
                        help="Directories containing Asterix data " \
                            "to reduce. Each directory listed is " \
                            "assumed to contain one subdirectory " \
                            "for each frequency sub-band. Each " \
                            "directory listed is reduced independently.")
    parser.add_argument("-P", "--num-procs", dest='numproc', type=int, \
                        default=1, \
                        help="Number of processes to run simultaneously.")
    args = parser.parse_args()
    main()
