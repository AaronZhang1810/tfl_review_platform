# Public media provenance

The PNG screenshots in `docs/images/` and the MP4 walkthrough in `docs/media/` were generated from the repository's fictional `SYN-101` demo on 2026-08-09. They show synthetic documents, counts, findings, decisions, and model behavior and were visually reviewed before inclusion. They contain no patient, sponsor, client, employee, production-project, credential, or personal-path data.

Their SHA-256 digests are pinned in `public_release.py`. The public-source audit fails if any approved binary changes. Updating an image or video therefore requires:

1. regenerating it only from the isolated synthetic demo;
2. visually reviewing every frame or image for disclosure;
3. updating the corresponding digest in `APPROVED_BINARY_SHA256`;
4. rerunning tests and `python public_release.py --check`; and
5. completing the separate rights and publication review.

The media demonstrate application behavior; they are not evidence of performance on real clinical documents.
