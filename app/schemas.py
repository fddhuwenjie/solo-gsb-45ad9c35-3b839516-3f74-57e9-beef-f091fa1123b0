"""Request/response models."""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class CellIn(BaseModel):
    id: str = Field(min_length=1, description="电芯 ID")
    chemistry: str = Field(min_length=1, description="化学体系，如 LFP / NMC")
    capacity_ah: float = Field(gt=0, description="标称容量 (Ah)")
    resistance_mohm: float = Field(ge=0, description="内阻 (mΩ)")
    test_version: str = Field(min_length=1, description="检测版本，可后续修订")


class CellsBulkIn(BaseModel):
    cells: List[CellIn] = Field(min_length=1, max_length=12, description="本次录入的电芯，最多 12 节")

    @model_validator(mode="after")
    def _unique_ids(self) -> "CellsBulkIn":
        ids = [c.id for c in self.cells]
        if len(set(ids)) != len(ids):
            raise ValueError("cells 内不允许出现重复电芯 ID")
        return self


class RevisionIn(BaseModel):
    test_version: str = Field(min_length=1, description="新的检测版本")


class PreselectIn(BaseModel):
    chemistry: str = Field(min_length=1, description="化学体系")
    min_size: int = Field(ge=3, le=6, description="串联最少节数 (3~6)")
    max_size: int = Field(ge=3, le=6, description="串联最多节数 (3~6)")
    min_capacity_ah: float = Field(gt=0, description="最低单芯容量 (Ah)")
    max_capacity_delta: float = Field(ge=0, description="组内容量差上限 (Ah)")
    max_resistance_delta: float = Field(ge=0, description="组内内阻差上限 (mΩ)")

    @model_validator(mode="after")
    def _check_size_range(self) -> "PreselectIn":
        if self.min_size > self.max_size:
            raise ValueError("min_size 不能大于 max_size")
        return self


class ConfirmIn(BaseModel):
    chemistry: str = Field(min_length=1)
    candidate_ids: List[str] = Field(min_length=3, max_length=6, description="预选返回的候选 ID")
    # 各芯检测版本：确认时据此重查；与库中不一致即判定候选过期
    versions: Dict[str, str]
    min_size: int = Field(ge=3, le=6)
    max_size: int = Field(ge=3, le=6)
    min_capacity_ah: float = Field(gt=0)
    max_capacity_delta: float = Field(ge=0)
    max_resistance_delta: float = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> "ConfirmIn":
        if self.min_size > self.max_size:
            raise ValueError("min_size 不能大于 max_size")
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("candidate_ids 不允许重复")
        missing = [cid for cid in self.candidate_ids if cid not in self.versions]
        if missing:
            raise ValueError(f"versions 缺少以下电芯的检测版本: {missing}")
        extra = [v for v in self.versions if v not in self.candidate_ids]
        if extra:
            raise ValueError(f"versions 包含不属于候选的电芯: {extra}")
        return self


# ---------- responses ----------

class CellOut(BaseModel):
    id: str
    chemistry: str
    capacity_ah: float
    resistance_mohm: float
    test_version: str
    occupied: bool
    occupied_group: Optional[int] = None


class PreselectConstraints(BaseModel):
    chemistry: str
    min_size: int
    max_size: int
    min_capacity_ah: float
    max_capacity_delta: float
    max_resistance_delta: float


class CandidateOut(BaseModel):
    candidate_ids: List[str]
    size: int
    limiting_capacity_ah: float
    capacity_spread_ah: float
    resistance_spread_mohm: float
    versions: Dict[str, str]
    constraints: PreselectConstraints


class PreselectResponse(BaseModel):
    feasible: bool
    candidate: Optional[CandidateOut] = None
    reason_code: Optional[str] = None
    reason: Optional[str] = None
    pool_size: int


class GroupSnapshotCell(BaseModel):
    position: int
    cell_id: str
    chemistry: str
    capacity_ah: float
    resistance_mohm: float
    test_version: str


class ConfirmResponse(BaseModel):
    group_no: int
    candidate_ids: List[str]
    limiting_capacity_ah: float
    versions: Dict[str, str]
    snapshot_cells: List[GroupSnapshotCell]


class GroupOut(BaseModel):
    group_no: int
    chemistry: str
    limiting_capacity_ah: float
    created_at: str
    cells: List[GroupSnapshotCell]
