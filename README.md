<h1 align="center">MKV Subtitle Manager</h1>

<p align="center">
  Extract and remove embedded subtitles from MKV files with batch processing support.
</p>

---

I built this tool to solve a specific issue: many embedded subtitles in MKV files either caused lag or failed to play properly in Jellyfin, mainly due to complex SSA scripts or bulky HDMV PGS subtitles. This tool extracts embedded subtitles and converts them into external `.srt` files, improving compatibility, reducing playback issues, and replacing less readable subtitle formats with cleaner text-based ones.

Feel free to recommend me any features i need to add and bugs to fix in the issues tab!

I havent tested on linux yet but i have tested on windows so if anyone wants to test my script on linux or make a linux variant of the script then that would be very much apprecieted.


### Dependencies
- Tesseract-OCR
- MKVToolNix
- Python dependencies: pytesseract, Pyside6, Fluentwidgets (pip install PySide6 "PyQt-Fluent-Widgets[full]" pytesseract) 


FYI: This tool is vibe coded.
