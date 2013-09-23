#!/usr/bin/env python2
#  written by Marc Lucke marc@marcsnet.com
#  please respect my time and try to solve any problem yourself before contacting me
#  if I can see you've made an effort, I'll be very happy to help
version='1.2.0a'

import imaplib, pickle, os, sys, email, time, configparser, MySQLdb, re, random, datetime, logging, math, pprint, warnings, hashlib
from nilsimsa import *
from optparse import OptionParser

config = configparser.ConfigParser()
config.read('imap_autosort.conf')
# set globals from conf
threshold=int(config['nilsimsa']['threshold'])
min_score=int(config['nilsimsa']['min_score'])
todo=config['imap']['todo']
new=config['imap']['new']
imap_folders=[x.strip() for x in str(config['imap']['folders']).split(',')]
weight_headers=[x.strip() for x in str(config['nilsimsa']['weight_headers']).split(',')]
weight_headers_by=int(config['nilsimsa']['weight_headers_by'])
mysql_pass=config['mysql']['password']
pp = pprint.PrettyPrinter(indent=2)

# I don't like this, but I'll clean it later & play now
# I will define a global hash to store the distances on the sync run because this script runs once per email message
# the hash structure will be distances["folder"]=(d1,d2,...dk,..dn)
distances={}


# MySQLdb_db='.imap_nilsimsa.db'
lockfile='nilsimsa.lock'
db_version='0.0' # default

# exclude headers & recived dates
exclude_headers=re.compile('^(Date|Message-ID|X-.*Mailscanner.*|X-Amavis-.*|X-Spam-.*|X-Virus-.*)$',re.I)
no_dates_received=re.compile(';\s+.*$',re.M | re.I)
chomp_header=re.compile('[\r\n]+\s*',re.M)
exclude_received_from_localhost=re.compile('^from\s+(localhost|marcsnet.com)\s+',re.I)

# generate compiled weight_headers match
weight_headers_pattern='^(' + '|'.join(weight_headers) + ')$'
weight_headers_re=re.compile(weight_headers_pattern,re.I)

def return_header(mail_txt):
  result=''
  msg=email.message_from_string(mail_txt)
  for header in sorted(set(msg.keys())):
    if not exclude_headers.match(header):
      for this_header_content in sorted(msg.get_all(header)):
        if header == 'Received' and exclude_received_from_localhost.match(this_header_content):
          continue
        if header == 'Received' or header == 'X-Received':
          add = header + ': ' + no_dates_received.sub('',this_header_content)
        else:
          add = header + ': ' + this_header_content
        add = chomp_header.sub(' ',add) + "\n"
        if weight_headers_re.match(header):
          add += add*weight_headers_by
        result += add
  return result

def status(num,max,message=''):
  # takes range argument so 10 elements would be 0..9
  if (max-1)<1: return
  percent = int(100*int(num)/int(max-1)+0.5)
  num_equals=int(percent/2)
  sys.stdout.write("%s [%-50s] %3d%% %s/%s\r" % (message,'=' * num_equals,percent,(num+1),max))
  if num==(max-1):
    print ""
  sys.stdout.flush()
  
def sync_and_distance(folder,source_hexdigest,dry_run,debug,quiet,stats):
  if not quiet:
    print "Analysing folder %s" % folder
  # this function has 3 goals
  # by traversing the sqlite db (which in effect is acting as a cache)
  # and traversing the imap folder
  # 1) sync imap folder with squlite db
  # 2) return the folder's score

  #init
  mail={}
  distances[folder]=[]
  # load the MySQLdb into a hash for this folder
  cursor.execute("select uid,hexdigest from nilsimsa where folder='%s'" % folder)
  for row in cursor:
    mail[row[0]]=row[1]
  # get the email_uids for folder
  imap.select('"'+folder+'"',readonly=False)
  result,data=imap.uid('search', None, "(SEEN)")
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
      trimmed_header=return_header(raw_header)
      md5sum=hashlib.md5(trimmed_header).hexdigest()
      # compute the hexdigest & get the distance
      try:
        nilsimsa=Nilsimsa(trimmed_header)
      except:
        # if we can't get the hex digest, there's no point continuing 
        continue
      # let's log where the message actually was
      cursor.execute("update nilsimsa set moved_to='%s' where md5sum='%s' and folder!='%s'" % (folder,md5sum,folder))
      target_hexdigest = nilsimsa.hexdigest()
      # store in the db
      if debug:
        print "storing email_uid %s into db" % email_uid
      query="insert into nilsimsa (uid,folder,hexdigest,md5sum,trimmed_header) values (%s,%s,%s,%s,%s)"
      cursor.execute(query,(email_uid,folder,target_hexdigest,md5sum,trimmed_header))
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
    stats.write(str(folder) + "," + str(distance) + "\r\n")
    if debug:
      print "calculated distance between %s and %s = %s" % (source_hexdigest,target_hexdigest,distance)
    # load this distance into the distance hash
    distances[folder].append(distance)
  
  # now mail should only have items in it that have been deleted from the imap folder
  # let's get them out of our db
  leftovers=mail.keys()
  num_leftovers=len(leftovers)
  for i in range(num_leftovers):
    email_uid=leftovers[i]
    if not quiet:
      status(i,num_leftovers,'deleting moved messages')
    if not dry_run:
      cursor.execute("delete from nilsimsa where uid=%d and folder='%s'" % (email_uid,folder))
      db_connect.commit()
    else:
      if dry_run:
        print "Dry run: would have deleted db entry uid: %s, folder: %s" % (email_uid,folder)
 
def score_this(folder,threshold,debug,quiet):
  # init
  score=0.0
  scored_count=0
  average=0
  # iterate through list of scores for this folder & calculate the score based on the given threshold
  for distance in distances[folder]:
    # calculate the score
    if distance > threshold:
      # the score should always be out of 100
      # weight closer matches parabolicly (is that a word? :))
      this_score = int((10*(distance-threshold)/(128-threshold))**2)
      score += this_score 
      scored_count += 1
      if debug:
        print "Score: %s count: %s" % (this_score,scored_count)
    else:
      if debug:
        print "no score, distance %s less than threshold %s" % (distance,threshold)
    
  # return the score for this folder
  if scored_count:
    average=score/scored_count
    ## a bit of a terrible hack - #fixme
    # we still want the score weighted to home many matches there are, but we want to play that down.  A lot.
    score=score*math.log10(scored_count)
    report_result="%s matches over threshold %s in folder: %s; score: %s; average: %s" % (scored_count,threshold,folder,score,average)
    logger.info(report_result)
  else:
    average='n/a: %s nothing over threshold %s' % (folder,threshold)
    report_result=average
  if not quiet:
    print report_result
  # log & return resutl
  return score,average

def todo_count():
  imap.select(todo, readonly = True)
  resp, data = imap.search(None, 'ALL')
  mycount = len(data[0].split())
  return mycount

def autosort_inbox(folders,dry_run=False,debug=False,quiet=False):
  global considered # yuck
  score=0.0
  average=0
  while todo_count():
    imap.select(todo, readonly = False)
    result, data = imap.uid('search', None, "(UNSEEN)")
    email_uids=[int(x) for x in data[0].decode().split()]
    for email_uid in email_uids:
      stats = open("stats/" + str(time.time()) + ".csv", 'w')
      # don't consider unread email we weren't previously able to move
      cursor.execute("select count(*) from considered where uid=%d" % (email_uid))
      for row in cursor: count=row[0]
      if count:
        if not quiet:
          print "Already considered email_uid %s" % email_uid
        continue
      if not quiet:
        print "\nAnalysing unread inbox message %s" % email_uid
      # get the nilsimsa hexdigest of the header of this unread message  
      imap.select(todo, readonly = False)
      result,data=imap.uid('fetch', email_uid, '(BODY.PEEK[HEADER])')
      if debug:
        print "result: %s data: %s" % (result,data)
      try:
        raw_header=data[0][1]
      except:
        print "Error: email_uid: %s has no data" % email_uid
        continue
      msg=email.message_from_string( raw_header )
      logger.info("-----")
      for item in ['Date','From','To','Subject','Message-Id']:
        logger.info(item+": "+re.sub(r"\r","",str(msg[item])))
      trimmed_header=return_header(raw_header)
      if not quiet:
        print "  Source: subject: %s" % msg['Subject']
      try:
        nilsimsa=Nilsimsa(trimmed_header)
      except:
        # if we can't get the hex digest, there's no point continuing 
        continue
      source_hexdigest = nilsimsa.hexdigest()
      # get the score for each imap folder & select the winnig imap account (if any)
      winning_folder=new
      winning_score=0
      for folder in folders:
        sync_and_distance(folder,source_hexdigest,dry_run,debug,quiet,stats)
      for folder in folders:
        score,average=score_this(folder,threshold,debug,quiet)
        if score and score>winning_score and score>min_score:
          winning_score=score
          winning_folder=folder
      stats.close()
      # move the message to the winning folder or default new
      if not dry_run:
        if not quiet:
          print "* moving message to %s" % winning_folder
        imap.select(todo, readonly = False)
        result=imap.uid('COPY',email_uid,'"'+ winning_folder +'"')
        if result[0]=='OK':
          mov, data=imap.uid('STORE',email_uid,'+FLAGS','(\\Deleted)')
          imap.expunge()
          logger.info("moved to: %s" % winning_folder)
      else:
        print "Dry run: would have moved %s to folder %s and stored that to the db" % (email_uid,winning_folder)

def prune_considered(reconsider_after):
  now=int(time.time())
  logger.info("Time "+str(time.time()))
  # we want to ensure that no one run has to reconsider all inbox emails at once..
  delete_older_than=now-reconsider_after-random.randint(0,reconsider_after)
  cursor.execute("delete from considered where considered_when < %d" % (delete_older_than))
  db_connect.commit()

def archive(folders,archive,after,dry_run,just_delete,trash):
  older=int(after)*24*60*60
  for folder in folders:
    imap.select(folder, readonly = False)
    result, data = imap.uid('search', None, "(SEEN OLDER %s)" % older)
    email_uids=[int(x) for x in data[0].decode().split()]
    for email_uid in email_uids:
      if not dry_run:
        if folder in just_delete:
          where=trash
        else:
          where=archive
        result=imap.uid('COPY',email_uid,'"'+where+'"')
        if result[0]=='OK':
          mov, data=imap.uid('STORE',email_uid,'+FLAGS','(\\Deleted)')
          imap.expunge()
      else:
        print "Dry run: message %s of folder %s would have been archived to %s" % (email_uid,folder,archive)

if __name__ == "__main__":
  # exit if we are under maintenance
  if eval(config['general']['maintenance']):
    sys.exit('Under Maintenance')
  # look for a lock file, if it exists, exit out quoting pid to stderr
  if os.path.exists(lockfile):
    f=open(lockfile)
    pid=int(f.readline().rstrip())
    f.close()
    print "Lockfile detected, PID=%s" % pid
    try:
      os.kill(pid, 0)
    except OSError as err:
      os.remove(lockfile)
      sys.exit("pid %s dead but lockfile exists - removing lockfile and exiting\n(%s}): %s" % (pid, err.errno, err.strerror))
    else:
      sys.exit()
  else:
    f=open(lockfile,'w')
    f.write(str(os.getpid()))
    f.close()

  # turn off mysql warnings
  warnings.filterwarnings('ignore', 'Table .* already exists')
    
  # connect to database, create table/s if necessary
  db_connect=MySQLdb.connect(host="localhost", user="imap_nilsimsa", passwd=mysql_pass, db="imap_nilsimsa");
  cursor=db_connect.cursor()
  cursor.execute('create table if not exists nilsimsa (id INTEGER PRIMARY KEY AUTO_INCREMENT , uid INTEGER, folder TEXT, hexdigest TEXT, md5sum TEXT, moved_to TEXT)')
  cursor.execute('create table if not exists considered (uid INTEGER, considered_when INTEGER)')
  cursor.execute('create table if not exists version (version TEXT)')
  cursor.execute('select version from version limit 1')
  for row in cursor: # yuck
    db_version=row[0]
  if db_version != version:
    print 'deleting database as the version is missing or incorrect.  It will take some time to rebuild the index, please be patient.'
    cursor.execute('drop table nilsimsa')
    cursor.execute('create table nilsimsa (id INTEGER PRIMARY KEY AUTO_INCREMENT , added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP , uid INTEGER, folder TEXT, hexdigest TEXT, md5sum TEXT, trimmed_header TEXT)')
    cursor.execute('delete from version')
    cursor.execute("insert into version (version) values ('%s')" % version)
  # parse options
  parser = OptionParser(usage="python imap_nilsimsa.py or --help", description=__doc__)
  parser.add_option("-d", "--debug", action="store_true", default=False, dest="debug", help="debug information")
  parser.add_option("-q", "--quiet", action="store_true", default=False, dest="quiet", help="supress informational output")
  parser.add_option("-l", "--loop", action="store", type="float", default=0.0, dest="loop", help="loop -l seconds")
  parser.add_option("--dry-run", action="store_true", default=False, dest="dry_run", help="don't actually do anything, just tell us what you would do")
  (options, args)= parser.parse_args()
  
  # logging
  logger = logging.getLogger('imap_nilsimsa')
  hdlr = logging.FileHandler( "%s.log" % time.strftime("%Y%m%d", time.localtime()) )
  formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
  hdlr.setFormatter(formatter)
  logger.addHandler(hdlr) 
  logger.setLevel(logging.INFO)
  # do it
  loop_count = 1
  while True:
    prune_considered(int(config['general']['reconsider_after']))
    if not options.quiet:
      print "\n-----\nProcessing: %s, cycle: %d" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),loop_count)

    # log in to the server
    try:
      imap=imaplib.IMAP4_SSL( config['imap']['server'] )
    except:
      raise

    try:
      imap.login(config['imap']['username'],config['imap']['password'] )
    except:
      raise

    # start with archiving mail (so we don't need to process it :)
    if not options.quiet:
      print "Archiving messages"
    try:
      if config['archive']['folder'] and config['archive']['after'] > 0:
        just_delete=None
        if config['archive']['justdelete'] and config['archive']['trash']:
          just_delete=[x.strip() for x in str(config['archive']['justdelete']).split(',')]
        archive(imap_folders,config['archive']['folder'],config['archive']['after'],options.dry_run,just_delete,config['archive']['trash'])
    except:
      pass


    if not options.quiet:
      print "Sorting mail"

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
