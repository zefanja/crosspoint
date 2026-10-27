# CrossPoint

A web app for memorizing Bible verses with spaced repetition (FSRS) – compatible with Anki.
Learn in the browser without installing anything, and export your cards **including your
learning progress** to Anki (Desktop, AnkiDroid, AnkiMobile) at any time.

- Card types: Bible verse, question & answer (Anki's Basic), and whole passages via the
  Bible Memorizer generator
- Groups: an admin distributes group decks to members and follows their progress
- Languages: English and German – more can be added (see [docs/translations.md](docs/translations.md))

Documentation:
- Requirements (German): [SPEC.md](SPEC.md)
- Development: [CLAUDE.md](CLAUDE.md)
- Deployment with Portainer: [docs/deploy.md](docs/deploy.md)

The "Bible Memorizer" note type and card generator come from
[zefanja/bible-memorizer](https://github.com/zefanja/bible-memorizer) (GPL-3.0).

License: AGPL-3.0 (see [LICENSE](LICENSE)), because the `anki` library used here is AGPL-3.0.
