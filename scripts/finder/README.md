# Finder

This MIT-licensed ISBN finder is included with Topclass. Its origin, pinned revision, and copied-file hashes are in `origin.json`; attribution remains in `LICENSE`. Public setup needs no private repository access.

Run `./topclass setup` from the Topclass root. It installs locked Node dependencies, builds the server, and prepares Chromium. Confirmed education automatically invokes `search.py` with supported ISBNs. Topclass keeps results and indexing queues private to the hire.

Search checks ISBN metadata, catalog MD5, supported formats, and SHA-256 stability. It does not use ClamAV, execute downloaded files, or prove every file is the requested textbook. Source failures and browser verification remain explicit. Books without a supported ISBN retain their links.

Run `npm test` and `python3 -m unittest discover -s tests -p test_search_flow.py` here for local validation. Node.js 20+ is required. Browser sessions and generated `dist`/`node_modules` are not distributed.
