imap_autosort_nilsimsa
======================

This is a pre-alpha script - more of a concept if you like.  It actually works, but it is undergoing sporadic development.


The purpose is to use nilsimsa on email headers to automatically sort your inbox into imap folders.  It is built on Python 3 but wil work in Python 2 with minimal modification.

Use the imap_nilsima.conf for configuration; a example is provided: imap_nilmsa.conf.sample

For the IMAP folders you specify the script will build a database of nilsimsa hex digests of the headers of all of the email in that folder.  When sorting, the script will look for unread email in your inbox, calculate the nilsimsa hexdigest, calculate the nilsimsa distance and use that to score where the email is moved to.

Nilsimsa provides a distance from -127 (2 bits of text are nothing alike) to 128 (2 bits of text are the same).  Through analysis of my email messages I find that the headers of like email score ~80 which I call the threshold.  If an email scores below 80 is is not scored.  The score is caluclated such that it is out of 100 per email on a factor of 2 (parabolic).  Those scores are then added together per folder.  Whichever folder has the highest score gets the mail.  This algorithm may be subject to tuning but thus far it works quite well.  The closer the email is to an email that exists in the target IMAP folder is, the higher it scores BUT if there are many more lower scoring emails in another folder then the score can be overriden. 

You don't need to understand the script to use it and you may have varying levels of success with it.  I encourage feedback to marc@marcsnet.com
