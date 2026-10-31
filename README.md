# rundfunk-feeds

Official podcast RSS feeds of the public broadcasters in Germany, Austria and
Switzerland (ARD, ZDF, Deutschlandradio, Deutsche Welle, ORF, SRG SSR), checked
weekly and published as a filterable page plus OPML for feed readers:
<https://kmein.github.io/rundfunk-feeds/>

## How it stays current

`update.py` runs every Monday in GitHub Actions. It

1. collects feed URLs from the indexes that list them machine-readably: the ARD
   Sounds API, the SRG integration layer (SRF, RTR), the Deutschlandradio
   podcast pages and the ORF audio API;
2. fetches every feed in `feeds.json` and every newly found URL;
3. adds new URLs that are valid podcast feeds and not an alias of a known one
   (most enclosures shared), and drops a known feed after it has failed four
   runs in a row;
4. drops shows from `missing.json` once ARD Sounds lists a working feed for
   them or they have been inactive for a year;
5. commits the changed lists and deploys `site/`.

Everything else in `feeds.json` – RTS, RSI, ZDF, DW, and the ARD shows whose
feed exists only on the station's own website – was found once by hand and is
only kept alive by the weekly check, not rediscovered. New feeds from those
broadcasters need a pull request.

Run it locally with `python3 update.py` (standard library only); the page ends
up in `site/`.

## Adding or fixing a feed

Edit `feeds.json`: one object per line with `broadcaster`, `station`, `title`,
`rss`. Only feeds published by the broadcaster itself belong here, not
third-party proxies that rebuild feeds from ARD Sounds. A show that exists only
in ARD Sounds goes into `missing.json` with a short reason.

## Caveats

- The episode counts are what the feed currently contains. Many broadcasters
  truncate their feeds, so this is not the size of the archive.
- Some station feeds lag behind ARD Sounds or have stopped while the show goes
  on there.
- Broadcaster CDNs throttle bulk requests; Deutschlandradio answers 403 after a
  burst, so `update.py` spaces requests per host.
