from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["greenhouse", "lever", "ashby", "workday", "smartrecruiters", "custom"]
    enabled: bool = True
    board_token: str | None = None
    board_name: str | None = None
    site: str | None = None
    region: str = "global"
    endpoint: str | None = None
    tenant: str | None = None
    workday_cluster: str | None = None
    site_id: str | None = None

    @model_validator(mode="after")
    def validate_required_identifier(self) -> "SourceConfig":
        if not self.enabled:
            return self
        required = {
            "greenhouse": ("board_token",),
            "lever": ("site",),
            "ashby": ("board_name",),
            "workday": ("tenant", "workday_cluster", "site_id"),
        }
        missing = [name for name in required.get(self.type, ()) if not getattr(self, name)]
        if missing:
            raise ValueError(f"{self.type} requires {', '.join(missing)}")
        return self


class CompanyConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str
    enabled: bool = True
    product_company: bool = False
    sources: list[SourceConfig]


class CompaniesConfig(BaseModel):
    companies: list[CompanyConfig]

    @model_validator(mode="after")
    def unique_ids(self) -> "CompaniesConfig":
        ids = [company.id for company in self.companies]
        if len(ids) != len(set(ids)):
            raise ValueError("company IDs must be unique")
        return self


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def load_companies(path: Path) -> CompaniesConfig:
    return CompaniesConfig.model_validate(load_yaml(path))
