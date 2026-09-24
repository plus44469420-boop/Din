# Settlement claim desk

A local page for tracking class-action settlements mentioned around [r/classactions](https://www.reddit.com/r/classactions/).

Listings are sorted into **no proof** and **needs proof**. A claim packet opens only after you check **I’ve been there** and confirm you meet that settlement’s class definition. The desk copies your settlement-only contact details and links to a recommended listing site. You submit the official administrator form yourself.

Nothing in this app files a claim on a settlement website.

## Run

```bash
python3 -m http.server 4173
```

Open `http://127.0.0.1:4173`.

Profile, “I’ve shopped there” notes, and the completed list stay in this browser’s local storage.

## Sources

The catalog cross-checks dates and proof flags against public listing sites the subreddit points to: Top Class Actions, ClassAction.org, OpenClassActions, ClaimDepot, and Settlement Pulse. Confirm every deadline on the official claim site before you file. Do not pay a service to submit a claim.
