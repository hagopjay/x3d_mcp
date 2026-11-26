"""Stage 2 of Q12 (Python 3.12 or older: omniverse-asset-validator 1.18.0 needs it). Run the executable USD rulebooks (usd-core UsdValidation, Omniverse Asset Validator) on each .usdc,
one rule at a time, each guarded: a rule that raises is reported as "not run". Python 3.12 or older."""
import sys, json, glob, os, logging, importlib.metadata as md
logging.disable(logging.CRITICAL)
from pxr import Usd, UsdValidation
import omni.asset_validator as av

out_dir, dest = sys.argv[1], sys.argv[2]
reg = UsdValidation.ValidationRegistry()
usd_vals = {"usd:" + v.GetMetadata().name: v for v in reg.GetOrLoadAllValidators()}
oav_rules = {"oav:" + n: getattr(av, n) for n in dir(av) if n.endswith("Checker") or n in ("UsdDanglingMaterialBinding", "UsdMaterialBindingApi", "UsdzPackageValidator")}
PINNED = {r["id"] for r in json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "usd_rules.json")))["rules"]}
oav_rules = {k: v for k, v in oav_rules.items() if k in PINNED}
res, versions = {}, {"usd-core": md.version("usd-core"), "omniverse-asset-validator": md.version("omniverse-asset-validator"), "python": sys.version.split()[0]}
for f in sorted(glob.glob(os.path.join(out_dir, "*.usdc"))):
    n = os.path.basename(f)[:-5]; r = {}
    stage = Usd.Stage.Open(f)
    for rid, v in usd_vals.items():
        try:
            errs = v.Validate(stage); r[rid] = "fail" if errs else "pass"
        except Exception as e:
            r[rid] = "not-run: " + str(e)[:80]
    for rid, cls in oav_rules.items():
        try:
            if not (isinstance(cls, type) and issubclass(cls, av.BaseRuleChecker)): continue
            e = av.ValidationEngine(init_rules=False); e.enable_rule(cls)
            issues = e.validate(f).issues()
            r[rid] = "fail" if any(i.severity in (av.IssueSeverity.ERROR, av.IssueSeverity.FAILURE, av.IssueSeverity.WARNING) for i in issues) else "pass"
        except Exception as ex:
            r[rid] = "not-run: " + str(ex)[:80]
    res[n] = r
json.dump({"versions": versions, "scenes": res}, open(dest, "w"), indent=1)
ids = {k for r in res.values() for k in r}
print(len(res), "scenes,", len(ids), "rules;", sum(1 for r in res.values() for v in r.values() if v.startswith("not-run")), "not run")
