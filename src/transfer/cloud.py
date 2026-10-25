"""In-memory form of a Gaussian splat set, in the units the X3D 4.1 GaussianSplats node uses."""

from dataclasses import dataclass, field

import numpy as np


class SplatError(ValueError):
    """A splat file or array is unreadable, truncated, over a limit or not what it claims to be."""


# X3D 4.1 GaussianSplats has sphericalHarmonicsDegree{d}Coef{c} fields for d = 1..3 only.
X3D_SH_MAX_DEGREE = 3
SH_DIMS = {0: 0, 1: 3, 2: 8, 3: 15, 4: 24}   # higher-order coefficients per colour channel, by degree


def sh_degree_for_dim(dim: int) -> int:
    """SH degree whose coefficient count (excluding the DC term) is `dim`."""
    for degree, d in SH_DIMS.items():
        if d == dim:
            return degree
    raise SplatError(f"{dim} SH coefficients per channel matches no degree (3, 8, 15 or 24 expected)")


def sh_field_names(degree: int) -> list[str]:
    """X3D field names for SH coefficients, in storage order (index k of `SplatCloud.sh`)."""
    return [f"sphericalHarmonicsDegree{d}Coef{c}"
            for d in range(1, min(degree, X3D_SH_MAX_DEGREE) + 1) for c in range(2 * d + 1)]


@dataclass
class SplatCloud:
    """One splat per row. All arrays are float32 and already in X3D units.

    positions   (n, 3)     metres in the file's own frame
    scales      (n, 3)     linear standard deviations (a 3DGS PLY stores ln(scale); this is exp of it)
    orientations (n, 4)    unit quaternion, x y z w (a 3DGS PLY stores w x y z)
    opacities   (n,)       0 to 1 (a 3DGS PLY stores logit(opacity); this is the sigmoid of it)
    dc          (n, 3)     SH degree 0 coefficient, sphericalHarmonicsDegree0Coef0
    sh          (n, k, 3)  higher-order coefficients in the order of sh_field_names(), or None
    """

    positions: np.ndarray
    scales: np.ndarray
    orientations: np.ndarray
    opacities: np.ndarray
    dc: np.ndarray
    sh: np.ndarray | None = None
    source_format: str = ""                      # "spz-3", "ply", ...
    info: dict = field(default_factory=dict)     # header facts worth reporting (version, flags, ...)

    def __len__(self) -> int:
        return int(self.positions.shape[0])

    @property
    def sh_degree(self) -> int:
        return 0 if self.sh is None else min(sh_degree_for_dim(self.sh.shape[1]), X3D_SH_MAX_DEGREE)

    def arrays(self) -> dict[str, np.ndarray]:
        """Field name -> (n, tuple) array, ready for text formatting (opacities as (n, 1))."""
        out = {"positions": self.positions, "scales": self.scales, "orientations": self.orientations,
               "opacities": self.opacities.reshape(-1, 1), "sphericalHarmonicsDegree0Coef0": self.dc}
        if self.sh is not None:
            for k, name in enumerate(sh_field_names(self.sh_degree)):
                out[name] = self.sh[:, k, :]
        return out

    def finite_mask(self) -> np.ndarray:
        """True for each splat whose every value is a finite number."""
        ok = (np.isfinite(self.positions).all(1) & np.isfinite(self.scales).all(1) & np.isfinite(self.orientations).all(1)
              & np.isfinite(self.opacities) & np.isfinite(self.dc).all(1))
        if self.sh is not None:
            ok &= np.isfinite(self.sh).all((1, 2))
        return ok

    def take(self, index: np.ndarray) -> "SplatCloud":
        """The splats at `index` (an index or boolean array) as a new cloud."""
        return SplatCloud(self.positions[index], self.scales[index], self.orientations[index],
                          self.opacities[index], self.dc[index],
                          None if self.sh is None else self.sh[index], self.source_format, dict(self.info))
