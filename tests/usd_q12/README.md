# Q12: X3D scenes through Blender to the USD rulebooks, joined per rule

    python tests/usd_q12/dump_scenes.py scenes                    # any Python with lxml
    python tests/usd_q12/export_blender.py scenes out ADDON/source export.json   # Python 3.13 + `pip install bpy==5.3.0`
    python tests/usd_q12/run_rulebooks.py out rulebooks.json      # Python 3.12 + usd-core + omniverse-asset-validator==1.18.0
    python tests/usd_q12/join.py scenes export.json rulebooks.json receipt.json --blender-addon-commit SHA

Result and reading: `docs/USD-BLENDER-JOIN.md`.
