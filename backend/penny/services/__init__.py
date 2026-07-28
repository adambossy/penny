"""Per-process service singletons.

The ported services (Taxonomy, MerchantRulesLoader, PersistTool, Categorizer,
MigrationTool) each load some state on construction; building them once per
process keeps the agent's tool calls cheap.
"""

from __future__ import annotations

from penny.db import get_db
from penny.rules.loader import MerchantRulesLoader, TaxonomyRulesLoader
from penny.taxonomy.core import Taxonomy
from penny.taxonomy.loader import load_taxonomy_from_db
from penny.tools._services.categorizer import Categorizer
from penny.tools._services.migrator import MigrationTool
from penny.tools._services.persister import PersistTool
from penny.workspace import resolve_memory_dir

_taxonomy: Taxonomy | None = None
_rules_loader: MerchantRulesLoader | None = None
_taxonomy_rules_loader: TaxonomyRulesLoader | None = None
_persister: PersistTool | None = None
_migrator: MigrationTool | None = None


def get_taxonomy() -> Taxonomy:
    global _taxonomy
    if _taxonomy is None:
        _taxonomy = load_taxonomy_from_db(get_db())
    return _taxonomy


def get_rules_loader() -> MerchantRulesLoader:
    global _rules_loader
    if _rules_loader is None:
        memory_dir = resolve_memory_dir()
        memory_dir.mkdir(parents=True, exist_ok=True)
        _rules_loader = MerchantRulesLoader(
            memory_dir / "merchant-rules.md",
            taxonomy=get_taxonomy(),
        )
    return _rules_loader


def get_taxonomy_rules_loader() -> TaxonomyRulesLoader:
    global _taxonomy_rules_loader
    if _taxonomy_rules_loader is None:
        memory_dir = resolve_memory_dir()
        memory_dir.mkdir(parents=True, exist_ok=True)
        _taxonomy_rules_loader = TaxonomyRulesLoader(memory_dir / "taxonomy-rules.md")
    return _taxonomy_rules_loader


def get_persister() -> PersistTool:
    global _persister
    if _persister is None:
        _persister = PersistTool(get_db(), get_taxonomy())
    return _persister


def build_categorizer() -> Categorizer:
    """A fresh ``Categorizer`` per call.

    Inexpensive to construct; keeping it per-call sidesteps any stale-state
    surprises if the taxonomy or rules file is hot-edited during a session.
    """
    return Categorizer(get_taxonomy(), rules_loader=get_rules_loader())


def get_migrator() -> MigrationTool:
    """One ``MigrationTool`` per process.

    Construction needs a ``Categorizer`` for split's constrained
    recategorization; ``merge`` no longer consults it.
    """
    global _migrator
    if _migrator is None:
        _migrator = MigrationTool(get_db(), get_taxonomy(), build_categorizer())
    return _migrator
