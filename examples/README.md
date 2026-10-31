# examples — a sample project to load into a fresh plm

`camper-ipad-dock/` is a real project carried out of the author's own plm:
a seat-side iPad charging dock for a truck camper, as a
[`plm.carry`](../plm/carry.py) bundle (six items, their git histories, the
meshes as LFS objects, every context note and check result, and one open
pull request). It is the project in the README's screenshots. Actor labels and links were
normalized for public distribution; they are not original authorship evidence.

```sh
docker compose up -d --build     # the stock instance (DEPLOY.md § Run it anywhere)
scripts/load-example.sh          # ~30 s; refuses to run twice
open http://localhost:8097
```

What you get:

| | |
|---|---|
| `CID-ASM-0001` | the dock assembly — exploded view, BOM, releases v0.1.0 and v0.1.1 |
| `CID-PRT-0001` … `0004` | cradle, USB-C plug carrier, its clamp cap, a mounting stand-off |
| `CID-CPN-0001` | a fit coupon: the gauge printed to check the iPad and keyboard fit |
| `CCR-AST-0001` | the camper the dock mounts to — created on the way in, no files |
| pull request **#380** | "Taller cradle side guides (+20 mm)": open, submitted, checks run — approve it as `admin` and watch the merge tag `v0.1.2` |
| pull request #379 | merged: the strap and captive charging lead, with its reviews and dispositions |

The bundle is loaded as the admin (`PLM_ADMIN` in `.env`, default `admin`);
the loader temporarily stops running services to avoid concurrent SQLite writes
during verification, then restarts them even if import is refused.
To refresh the sample from a running plm:

```sh
python -m plm.carry export --project camper-ipad-dock --from <db> \
    --repos <repos> --lfs <lfs> --instance <name> --out examples/camper-ipad-dock
```
