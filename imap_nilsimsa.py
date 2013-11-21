import imaplib, pickle, os, sys, email, time, configparser, sqlite3, re
from nilsimsa import *
from optparse import OptionParser

config = configparser.ConfigParser()
config.read('imap_autosort.conf')

imap_folders=[x.strip() for x in config['imap']['folders'].split(',')]
sqlite3_db='.imap_nilsimsa.db'
lockfile='nilsimsa.lock'

# get rid of patterns...
no_MailScanner=re.compile('^X-.*-MailScanner.*?: .*$',re.M | re.I)
no_amavis_1=re.compile('^X-Amavis.*?: .*$',re.M | re.I)
no_amavis_2=re.compile('^X-Virus-Scanned: .*$',re.M | re.I)
no_date=re.compile('^Date: .*$',re.M | re.I)
no_message_id=re.compile('^Message-ID: .*$',re.M | re.I)
no_spam_status=re.compile('^X-Spam-Status: .*$',re.M | re.I)
no_dates_received=re.compile('^(Received: .*?); .*$',re.M | re.I)

def remove_unwanted_in_header(header_string):
    header_string=header_string.replace('\r','')
    header_string=re.sub('\n[\t ]+',' ',header_string)
    header_string=no_MailScanner.sub('',header_string)
    header_string=no_amavis_1.sub('',header_string)
    header_string=no_amavis_2.sub('',header_string)
    header_string=no_date.sub('',header_string)
    header_string=no_message_id.sub('',header_string)
    header_string=no_spam_status.sub('',header_string)
    header_string=no_dates_received.sub(r'\1',header_string)
    return re.sub('\n{2,}','\n',header_string)

def status(num,max,message=''):
    # takes range argument so 10 elements would be 0..9
    if (max-1)<1: return
    percent = int(100*int(num)/int(max-1)+0.5)
    num_equals=int(percent/2)
    sys.stdout.write("%s [%-50s] %3d%% %s/%s\r" % (message,'=' * num_equals,percent,(num+1),max))
    if num==(max-1):
        print ""
    sys.stdout.flush()
    
def sync_and_score(folder,source_hexdigest,dry_run=False,debug=False,quiet=False):
    if not quiet:
        print "Analysing folder %s" % folder
    # this function has 3 goals
    # by traversing the sqlite db (which in effect is acting as a cache)
    # and traversing the imap folder
    # 1) sync imap folder with squlite db
    # 2) return the folder's score

    #init
    mail={}
    score=0.0
    scored_count=0
    # load the sqlite3 into a hash for this folder
    cursor.execute("select uid,hexdigest from nilsimsa where folder=?",[folder])
    for row in cursor:
        mail[row[0]]=row[1]
    # get the email_uids for folder
    imap.select('"'+folder+'"',readonly=False)
    result,data=imap.uid('search', None, "ALL")
    email_uids=[int(x) for x in data[0].decode().split()]
    # iterate through the email_ids
    message_count=len(email_uids)
    for i in range(message_count):
        email_uid=email_uids[i]
        if not quiet:
            status(i,message_count,'comparing')
        if debug:
            print "folder: %s email_uid: %s" % (folder,email_uid)
        # do we already know about this email?
        if not mail.has_key(email_uid):
            if debug:
                print "email_uid %s not in db: " % (email_uid)
            # get the email header
            result, data = imap.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
            raw_header = data[0][1]
            trimmed_header=remove_unwanted_in_header(raw_header)
            # compute the hexdigest & get the distance
            try:
                nilsimsa=Nilsimsa(trimmed_header)
            except:
                # if we can't get the hex digest, there's no point continuing 
                continue
            target_hexdigest = nilsimsa.hexdigest()
            # store in the db
            if debug:
                print "storing email_uid %s into db" % email_uid
            cursor.execute('insert into nilsimsa (uid,folder,hexdigest) values (?,?,?)',[email_uid,folder,target_hexdigest])
            db_connect.commit()
        else:
            if debug:
                print "email_uid %s in db, removing from deletion" % email_uid
            # yay - we know, we can simply compare...
            target_hexdigest=mail[email_uid]
            # because this is email_uid is known to the db, remove it from the dictionary
            # as we want to be left with only email_uids that are no longer in the IMAP folder
            del mail[email_uid]
        # calculate the nilsimsa distance
        distance=compare_hexdigests(source_hexdigest,target_hexdigest)
        if debug:
            print "calculated distance between %s and %s = %s" % (source_hexdigest,target_hexdigest,distance)
        # calculate the score
        if distance > threshold:
            # the score should always be out of 100
            # I want to weight higher matches parabolically
            this_score=(10*(distance-threshold)/(128-threshold))**2
            score+=this_score 
            scored_count+=1
            if debug:
                print "Score: %s count: %s" % (this_score,scored_count)
        else:
            if debug:
                print "no score, distance %s less than threshold %s" % (distance,threshold)
        
    # now mail should only have items in it that have been deleted from the imap folder
    # let's get them out of our db
    leftovers=mail.keys()
    num_leftovers=len(leftovers)
    for i in range(num_leftovers):
        email_uid=leftovers[i]
        if not quiet:
            status(i,num_leftovers,'deleting moved messages')
        if not dry_run:
            cursor.execute('delete from nilsimsa where uid=? and folder=?',[email_uid,folder])
            db_connect.commit()
        else:
            if dry_run:
                print "Dry run: would have deleted db entry uid: %s, folder: %s" % (email_uid,folder)
    
    # return the score for this folder
    if scored_count:
        average=score/scored_count
    else:
        average='n/a: no nothing over threshold %s' % threshold
    if not quiet:
        print "Score for folder %s=%s, average=%s" % (folder,score,average)
    return score

def autosort_inbox(folders,dry_run=False,debug=False,quiet=False):
    global considered # yuck
    imap.select('inbox', readonly = False)
    result, data = imap.uid('search', None, "(UNSEEN)")
    email_uids=[int(x) for x in data[0].decode().split()]
    for email_uid in email_uids:
        # don't consider unread email we weren't previously able to move
        cursor.execute('select count(*) from considered where uid=?',[email_uid])
        for row in cursor: count=row[0]
        if count:
            if not quiet:
                print "Already considered email_uid %s" % email_uid
            continue
        if not quiet:
            print "\nAnalysing unread inbox message %s" % email_uid
        # get the nilsimsa hexdigest of the header of this unread message    
        imap.select('inbox', readonly = False)
        result,data=imap.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
        if debug:
            print "result: %s data: %s" % (result,data)
        try:
            raw_header=data[0][1]
        except:
            print "Error: email_uid: %s has no data" % email_uid
            continue
        msg=email.message_from_string( raw_header )
        trimmed_header=remove_unwanted_in_header(raw_header)
        if not quiet:
            print "  Source: subject: %s" % msg['Subject']
        try:
            nilsimsa=Nilsimsa(trimmed_header)
        except:
            # if we can't get the hex digest, there's no point continuing 
            continue
        source_hexdigest = nilsimsa.hexdigest()
        # get the score for each imap folder & select the winnig imap account (if any)
        winning_folder=None
        winning_score=0
        for folder in folders:
            score=sync_and_score(folder,source_hexdigest,dry_run,debug,quiet)
            if score and score>winning_score:
                winning_score=score
                winning_folder=folder
        # if there's a winning folder, move the message to it & add it to the db
        if winning_folder:
            if not dry_run:
                if not quiet:
                    print "* moving message to %s" % winning_folder
                imap.select('inbox', readonly = False)
                result=imap.uid('COPY',email_uid,'"'+ winning_folder +'"')
                if result[0]=='OK':
                    mov, data=imap.uid('STORE',email_uid,'+FLAGS','(\Deleted)')
                    imap.expunge()
            else:
                print "Dry run: would have moved %s to folder %s and stored that to the db" % (email_uid,winning_folder)
        else:
            cursor.execute('insert into considered (uid,considered_when) values (?,?)',[email_uid,int(time.time())])
            db_connect.commit()
            if not quiet:
                print "- not moving message email_uid, email moved to considered list for %s seconds" % config['general']['reconsider_after']

def prune_considered():
    now=int(time.time())
    delete_older_than=now-int(config['general']['reconsider_after'])
    cursor.execute('delete from considered where considered_when < ?',[delete_older_than])
    db_connect.commit()

if __name__ == "__main__":
    # look for a lock file, if it exists, exit out quoting pid to stderr
    if os.path.exists(lockfile):
        f=open(lockfile)
        pid=f.readline()
        f.close()
        try:
            os.kill(pid, 0)
        except:
            os.remove(lockfile)
            sys.exit('pid %s dead but lockfile exists; removing lockfile and exiting' % pid)
        sys.exit('pid %s is running, exiting' % pid)
    else:
        f=open(lockfile,'w')
        f.write(str(os.getpid()))
        f.close()
        
    # connect to sqlitedb, create table/s if necessary
    db_connect=sqlite3.connect(sqlite3_db)
    cursor=db_connect.cursor()
    cursor.execute('create table if not exists nilsimsa (id INTEGER PRIMARY KEY AUTOINCREMENT , uid INTEGER, folder TEXT, hexdigest TEXT)')
    cursor.execute('create index if not exists"main"."folder_index" on "nilsimsa" ("folder" ASC)')
    cursor.execute('create table if not exists considered (uid INTEGER, considered_when INTEGER)')
    # parse options
    parser = OptionParser(usage="python imap_nilsimsa.py or --help", description=__doc__)
    parser.add_option("-d", "--debug", action="store_true", default=False, dest="debug", help="debug information")
    parser.add_option("-q", "--quiet", action="store_true", default=False, dest="quiet", help="supress informational output")
    parser.add_option("-l", "--loop", action="store", type="float", default=0.0, dest="loop", help="loop -l seconds")
    parser.add_option("--dry-run", action="store_true", default=False, dest="dry_run", help="don't actually do anything, just tell us what you would do")
    (options, args)= parser.parse_args()
    threshold=int(config['nilsimsa']['threshold'])
    # do it
    loop_count = 1
    while True:
        prune_considered()
        if not options.quiet:
            print "\n-----\nProcessing: %s, cycle: %d" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),loop_count)
        # log in to the server
        imap=imaplib.IMAP4_SSL( config['imap']['server'] )
        imap.login(config['imap']['username'],config['imap']['password'] )
        # sort inbox mail into imap folders
        autosort_inbox(imap_folders,options.dry_run,options.debug,options.quiet)
        # close connections & expunge etc.
        imap.close()
        imap.logout()
        # loop if asked for, stop if not
        if options.loop > 0:
            if not options.quiet:
                print "sleeping for %d seconds" % options.loop
            time.sleep(options.loop)
        else:
            break                        
        loop_count += 1
    # disconnect from squlite db
    db_connect.close()
    # delete lockfile
    os.remove(lockfile)