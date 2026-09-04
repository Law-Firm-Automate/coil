"""Feature map: every Coil feature listed under the company whose feature it mirrors."""
from flask import Blueprint, render_template
from ..feature_map import FEATURE_MAP
from ..helpers import login_required

bp = Blueprint("features", __name__, url_prefix="/features")


@bp.route("")
@login_required
def index():
    total = sum(len(g["features"]) for g in FEATURE_MAP)
    built = sum(1 for g in FEATURE_MAP for f in g["features"] if f[2] == "built")
    return render_template("features/index.html", groups=FEATURE_MAP, total=total, built=built)
