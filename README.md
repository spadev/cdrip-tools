cdrip-tools
===========
command-line accuraterip verifier and rip offset fixer

Files
-----
*arverify.py*

* Accuraterip verifier that supports multiple lossless formats via sox and ffmpeg
* Supports offset detection and both accuraterip v1 and v2
* Additionally supports specifying additional pregap samples
  as well as data track length in order to get the correct disc id
  from the accuraterip database

*fixoffset.py*

* Companion program to fix the offset of a rip

*splitaudio.c*

* Small libsdnfile C99 program to split raw audio read from stdin

*ckcdda.c*

* Adapted from https://github.com/jonls/accuraterip-tools
* Does the actual accuraterip checksum calculations (v1, v2, and offset detection)

Dependencies
------------
*mandatory*

* sox
* ffmpeg
* libsndfile

*optional*

* metaflac
* libsox-fmt-ffmpeg

*Ubuntu 12.04*

```aptitude install sox ffmpeg libsndfile1-dev flac libsox-fmt-ffmpeg```

Notes
-----

* Mostly tested and designed for Linux, but works on OS X for sure (though getting ffmpeg support for sox is probably a pain, and that's required for handling alac). Will probably work on Windows -- haven't tested at all, but I tried to minimize dependencies...
* All processing is done via pipes so no temp files

License
-------
GPLv3
