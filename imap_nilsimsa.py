import imaplib, pickle, sys, email, time, configparser, sqlite3
from nilsimsa import *
from optparse import OptionParser

config = configparser.ConfigParser()
config.read('imap_autosort.conf')

imap_folders = [x.strip() for x in config['imap']['folders'].split(',')]
threshold = float(config['nilsimsa']['match_threshold'])
sqlite3_db = '.imap_nilsimsa.db'
twirly = ('|','/','-','\\')

def status(num,max,message=''):
    if not max: return
    percent = int(100*int(num)/int(max)+1)
    num_equals=int(percent/2)
    sys.stdout.write("%s [%-50s] %3d%%\r" % (message,'=' * num_equals,percent))
    sys.stdout.flush()

def load_hexdigest(folder,debug=False):
    mail.select( '"'+folder+'"', readonly = True ) # connect to inbox.
    
    # get email_uids for this folder
    result, data = mail.uid('search', None, "ALL")
    email_uids = data[0].decode().split()

    # if an email has been deleted or moved from the imap folder, remove it from the db
    del_cursor = db_connect.cursor()
    del_cursor.execute('select id,uid from nilsimsa where folder=?',[folder])
    count = 0
    for row in del_cursor:
        # this is pretty annoying - fixme
        sys.stdout.write("Deletion anaylsis %s count=%6d\r" % (twirly[count % 4],count))
        sys.stdout.flush()
        count += 1
        
        if str(row[1]) not in email_uids:
            if debug:
                print ("removing uid: {} from folder: {} id: {}".format(row[1],folder,row[0]))
            cursor.execute('delete from nilsimsa where id=?',[row[0]])
    print()

    uid_count = len(email_uids)
    for i in range(uid_count):
        status(i,uid_count,'examining')
        continue
        email_uid = email_uids[i]
        # if we've seen this email_uid already, continue
        cursor.execute('select count(*) from nilsimsa where folder=? and uid=?',[folder,email_uid])
        row = cursor.fetchone()
        if row[0] != 0:
            if debug:
                print ("uid: {} seen - skipping".format(email_uid))
            continue
        else:
            print ("processing uid: {}".format(email_uid))
        
        # process header
        result, data = mail.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
        raw_header = data[0][1].decode('latin-1')
        try:
            nilsimsa = Nilsimsa( raw_header )
        except:
            continue
        hexdigest = nilsimsa.hexdigest()
        if debug:
            print ("{}: {}: {}".format(folder, email_uid, hexdigest))
        cursor.execute('insert into nilsimsa (uid,folder,hexdigest) values(?,?,?)',[email_uid,folder,hexdigest])
    db_connect.commit()

def autosort_inbox(folders,debug=False):
    mail.select('inbox', readonly = False)
    result, data = mail.uid('search', None, "(UNSEEN)")
    for email_uid in data[0].decode().split():
        print ("Considering inbox uid {}".format(email_uid))
        # get the nilsimsa hexdigest of this unread message
        result, data = mail.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
        raw_header = data[0][1].decode('latin-1')
        msg = email.message_from_string( raw_header )
        try:
            print("  Source: subject: {}".format(msg['Subject']))
        except:
            print( "Can't print the subject.  Python 2?" )
            pass
        try:
            print("  Source: from: {}".format(msg['From']))
        except:
            print( "Can't print the subject.  Python 2?" )
            pass
        try:
            print("  Source: to: {}".format(msg['To']))
        except:
            print( "Can't print the subject.  Python 2?" )
            pass
        try:
            nilsimsa = Nilsimsa( raw_header )
        except:
            continue
        source_hexdigest = nilsimsa.hexdigest()
        
        # get averages of all scores over threshold
        scores = {}
        for folder in folders: scores[ folder ] = 0.0
        for folder in folders:
            sum = 0.0
            count = 0.0
            cursor.execute('select hexdigest from nilsimsa where folder=?',[folder])
            for row in cursor:
                distance = compare_hexdigests(source_hexdigest, row[0])
                if distance > threshold:
                    if debug:
                        print ('folder: {} match {} is over {}'.format(folder,distance,threshold))
                    # the score should always be out of 100
                    # I want to weight higher matches parabolically
                    score = ( 10 * (distance - threshold) / ( 128 - threshold )) ** 2 
                    sum += score
                    count += 1
                    
                    # for debugging
                    if debug:
                        msg = email.message_from_string( raw_header )
                        try:
                            print("  Source: subject: {}".format(msg['Subject']))
                        except:
                            print( "Can't print the subject.  Python 2?" )
                            pass
                        try:
                            print("  Source: from: {}".format(msg['From']))
                        except:
                            print( "Can't print the subject.  Python 2?" )
                            pass
                        try:
                            print("  Source: to: {}".format(msg['To']))
                        except:
                            print( "Can't print the subject.  Python 2?" )
                            pass
            if count > 0:
                    scores [ folder ] = sum
                    average = sum / count
                    print ('{} messages scored over {} in folder {} - sum = {}, average = {}'.format(count,threshold,folder,sum,average))
            
        # which folder is the winner?
        winner = max(scores, key=scores.get)
        if scores [ winner ] > 0.0: # there's a fringe case where distance = threshold so the score is zero - will fight another day
            result = mail.uid('COPY', email_uid, '"'+ winner +'"')
            if result[0] == 'OK':
                mov, data = mail.uid('STORE', email_uid , '+FLAGS', '(\Deleted)')
                mail.expunge()
                print ("Message uid: {} moved to folder {}".format(email_uid,winner))
            else:
                print ("Message uid: {} could not be moved to folder {}".format(email_uid,winner))
        else:
            print ("Message uid: {} not moved".format(email_uid))
        print ("-----")
                    

if __name__ == "__main__":
    # connect to sqlitedb, create table/s if necessary
    db_connect=sqlite3.connect(sqlite3_db)
    cursor=db_connect.cursor()
    cursor.execute('create table if not exists nilsimsa (id INTEGER PRIMARY KEY AUTOINCREMENT , uid INTEGER, folder TEXT, hexdigest TEXT)')
    
    parser = OptionParser(usage="python imap_nilsimsa.py or --help", description=__doc__)
    parser.add_option("-r", "--rebuild", action="store_true", default=False, dest="rebuild", help="Rebuild (or build) the imap nilsimsa hex digests.  When used in combination with -l, will rebuild every -r loops")
    parser.add_option("-s", "--autosort", action="store_true", dest="sort", default=False, help="Sort email in the inbox")
    parser.add_option("-d", "--debug", action="store_true", default=False, dest="debug", help="debug information")
    parser.add_option("-l", "--loop", action="store", type="float", default=0.0, dest="loop", help="loop -l seconds")
    parser.add_option("--rebuild_every", action="store", type="float", default=10.0, dest="rebuild_every", help="rebuild every rebuild_every loop count")
    (options, args)= parser.parse_args()

    loop_count = 0.0
    while True:
        print ("Processing: {}".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
    
        # log in to the server
        mail = imaplib.IMAP4_SSL( config['imap']['server'] )
        mail.login( config['imap']['username'], config['imap']['password'] )
        
        if options.rebuild and not loop_count % options.rebuild_every:
            for imap_folder in imap_folders:
                print ("\nAnalysing " + imap_folder )
                load_hexdigest( imap_folder, options.debug )
        if options.sort:
            autosort_inbox( imap_folders, options.debug )
               
        if options.loop > 0:
            print ("Sleeping for {} seconds".format(options.loop))
            time.sleep(options.loop)
        else:
            break
            
        db_connect.close()
        mail.close()
        mail.logout()
            
        loop_count += 1
 