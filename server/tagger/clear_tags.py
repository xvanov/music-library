"""Strip tags from a file so the tagger re-derives them from its path.

Usage: clear_tags.py <path> [field ...]   (default: artist albumartist title)
"""
import sys

from mutagen.easyid3 import EasyID3

path = sys.argv[1]
keys = sys.argv[2:] or ["artist", "albumartist", "title"]
tags = EasyID3(path)
removed = []
for key in keys:
    if key in tags:
        del tags[key]
        removed.append(key)
tags.save()
print("cleared: " + (", ".join(removed) if removed else "nothing"))
