#!/usr/bin/perl
$|=1;
use Data::Dumper;

# where are the distance scores?
$stats="/home/hohum/imap_autosort_nilsimsa/stats";

# init
@files=();
@threed_files=();

chdir($stats);
opendir(STATS,".");
foreach $file(grep(-f && /\.csv$/ && !/^\./,readdir(STATS))) {
  push(@files,$file);
};
closedir(DIR);

@threed_files=grep(/\.score\.csv$/,@files);
map(s/\.score//,@threed_files);

for $file(@files) {
  next if $file ~~ @threed_files || $file=~/\.score\./;
  print "Considering $file\n";
  my %threed;
  $newfile=$file;
  $newfile=~s/\.csv$/.score.csv/;
  open(IN,$file);
  while(<IN>) {
    chomp; ~s/\r//g;
    my($folder,$distance)=split ',';
    next if $distance<50;
    if (defined($threed{$folder}{$distance})) {
      $threed{$folder}{$distance}++;
    }
    else {
      $threed{$folder}{$distance}=1;
    };
  };
  close(IN);
  open(OUT,">$newfile");
  for my $folder(sort(keys(%threed))) {
    for my $distance(sort(keys(%{$threed{$folder}}))) {
      print OUT $folder.",".$distance.",".$threed{$folder}{$distance}."\n";
    };
  };
  close(OUT);
};

