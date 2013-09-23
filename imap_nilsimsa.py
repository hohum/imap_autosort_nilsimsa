import imaplib, pickle, sys, email, time, configparser
from nilsimsa import *
from optparse import OptionParser

config = configparser.ConfigParser()
config.read('imap_autosort.conf')

imap_folders = [x.strip() for x in config['imap']['folders'].split(',')]

def load_hexdigest(folder,debug=False):
    if folder not in digests.keys(): digests[ folder ] = {}
    mail.select( '"'+folder+'"', readonly = True ) # connect to inbox.
    result, data = mail.uid('search', None, "ALL")
    email_uids = data[0].decode().split()
    deleted_uids = []
    for digest_uid in digests[ folder ].keys():
        if digest_uid not in email_uids: deleted_uids.append( digest_uid )
    for deleted_uid in deleted_uids:
            print ("Removing digest of deleted message {} in folder {}".format(deleted_uid,folder))
            del(digests[ folder ][ deleted_uid ])
    for email_uid in email_uids:
        if email_uid in digests[ folder ].keys(): continue
        result, data = mail.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
        raw_header = data[0][1].decode('latin-1')
        try:
            nilsimsa = Nilsimsa( raw_header )
        except:
            continue
        hexdigest = nilsimsa.hexdigest()
        if debug:
            print ("{}: {}: {}".format(folder, email_uid, hexdigest))
        digests[ folder ][ email_uid ] = hexdigest
    with open('nilsima_hexdigests.pickle', 'wb') as f:
        pickle.dump(digests, f, pickle.HIGHEST_PROTOCOL)

def autosort_inbox(folders,debug=False):
    mail.select('inbox', readonly = False)
    result, data = mail.uid('search', None, "(UNSEEN)")
    for email_uid in data[0].decode().split():
        print ("Considering inbox uid {}".format(email_uid))
        # get the nilsimsa hexdigest of this unread message
        result, data = mail.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
        raw_header = data[0][1].decode('latin-1')
        msg = email.message_from_string( raw_header )
        print("  Source: subject: {}".format(msg['Subject']))
        print("  Source: from: {}".format(msg['From']))
        print("  Source: to: {}".format(msg['To']))
        try:
            nilsimsa = Nilsimsa( raw_header )
        except:
            continue
        source_hexdigest = nilsimsa.hexdigest()
        
        # get averages of all scores over 80
        scores = {}
        for folder in folders: scores[ folder ] = 0.0
        for folder in folders:
            sum = 0.0
            count = 0.0
            for target_hexdigest in digests[ folder ].values():
                match = compare_hexdigests(source_hexdigest, target_hexdigest)
                if match >= 80:
                    if debug:
                        print ('folder: {} match {} is over 80'.format(folder,match))
                    # the idea is that the higher it is over 80, the more likely it is a match
                    # but if there's a lot of matches, that should count also
                    sum += 1 + match - float(config['nilsimsa']['match_threshold']) # the +1 ensures that one score of config['nilsimsa']['match_threshold'] is valid)
                    count += 1
                    msg = email.message_from_string( raw_header )
                    
                    # for debugging
                    if debug:
                        print("  Source: subject: {}".format(msg['Subject']))
                        print("  Source: from: {}".format(msg['From']))
                        print("  Source: to: {}".format(msg['To']))
            if count > 0:
                    scores [ folder ] = sum
                    print ('{} messages scored over 80 in folder {} - sum = {}'.format(count,folder,sum))
            
        # which folder is the winner?
        winner = max(scores, key=scores.get)
        if scores [ winner ] > 0.0:
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
    # if we've done this before, load in the data
    try: 
        with open('nilsima_hexdigests.pickle', 'rb') as f:
            digests = pickle.load(f)
    except: 
        digests={}
    
    parser = OptionParser(usage="python imap_nilsimsa.py or --help", description=__doc__)
    parser.add_option("-r", "--refresh", action="store_true", dest="refresh", default=False, help="Refresh (or build) the imap nilsimsa hex digests")
    parser.add_option("-s", "--autosort", action="store_true", dest="sort", default=False, help="Sort email in the inbox")
    parser.add_option("-d", "--debug", action="store_true", default=False, dest="debug", help="debug information")
    parser.add_option("-l", "--loop", action="store", type="float", default=0.0, dest="loop", help="loop -l seconds")
    (options, args)= parser.parse_args()

    do_loop = True
    while do_loop:
        print ("Processing: {}".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))      
        # log in to the server
        mail = imaplib.IMAP4_SSL( config['imap']['server'] )
        mail.login( config['imap']['username'], config['imap']['password'] )
        
        if options.refresh:
            for imap_folder in imap_folders:
                print ("Analysing " + imap_folder )
                load_hexdigest( imap_folder, options.debug )
        if options.sort:
            autosort_inbox( imap_folders, options.debug )
        
        mail.close()
        mail.logout()
        
        if options.loop > 0:
            print ("Sleeping for {} seconds".format(options.loop))
            time.sleep(options.loop)
        else:
            do_loop = False