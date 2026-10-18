"""Paketet ``db`` — SQLite-state för pipeline, utredning, karta, grafen och adminjobb.

Uppdelningen:

- ``_schema``: konstanter, schema, versionsstyrda migrationer, ``connect`` och
  ``init_schema`` samt gemensamma hjälpare.
- ``pipeline``: nedladdningar, per-PDF- och per-sida-status, kvalitet, ingest,
  LLM-korrigeringar, wpu-beslut och delta-queries.
- ``graph``: doc_entities, namnregler och grafgranskning.
- ``casebook``: utredningspärm, källbokmärken och marginalanteckningar.
- ``karta``: platser, observationer och kandidatgranskning.
- ``jobs``: admin_jobs.
- ``usage``: LLM-räknare per profil.

Konsumenter importerar fortfarande ``db`` och anropar ``db.<funktion>``. Alla
toppnivånamn i originalmodulen re-exporteras här, även privata (``_x``), så att
attributåtkomst och ``dir(db)`` fungerar som tidigare.
"""

from ._schema import (
    DEFAULT_DB as DEFAULT_DB,
)
from ._schema import (
    GRAPH_REVIEW_SCHEMA_SQL as GRAPH_REVIEW_SCHEMA_SQL,
)
from ._schema import (
    LLM_USAGE_SCHEMA_SQL as LLM_USAGE_SCHEMA_SQL,
)
from ._schema import (
    MIGRATIONS as MIGRATIONS,
)
from ._schema import (
    ROOT as ROOT,
)
from ._schema import (
    SCHEMA_SQL as SCHEMA_SQL,
)
from ._schema import (
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from ._schema import (
    ActiveAdminJobError as ActiveAdminJobError,
)
from ._schema import (
    DuplicateAdminJobError as DuplicateAdminJobError,
)
from ._schema import (
    InvalidAdminJobTransition as InvalidAdminJobTransition,
)
from ._schema import (
    _add_column_if_missing as _add_column_if_missing,
)
from ._schema import (
    _column_names as _column_names,
)
from ._schema import (
    _ensure_schema_version_table as _ensure_schema_version_table,
)
from ._schema import (
    _Migration as _Migration,
)
from ._schema import (
    _migration_006_pdf_file_ocr_status as _migration_006_pdf_file_ocr_status,
)
from ._schema import (
    _migration_007_admin_jobs as _migration_007_admin_jobs,
)
from ._schema import (
    _migration_008_graph_review as _migration_008_graph_review,
)
from ._schema import (
    _migration_009_graph_name_rules as _migration_009_graph_name_rules,
)
from ._schema import (
    _migration_010_llm_usage as _migration_010_llm_usage,
)
from ._schema import (
    _record_schema_version as _record_schema_version,
)
from ._schema import (
    _set_sqlite_user_version as _set_sqlite_user_version,
)
from ._schema import (
    _state_tables as _state_tables,
)
from ._schema import (
    _table_exists as _table_exists,
)
from ._schema import (
    connect as connect,
)
from ._schema import (
    init_schema as init_schema,
)
from ._schema import (
    now as now,
)
from ._schema import (
    schema_version as schema_version,
)
from ._schema import (
    source_for_path as source_for_path,
)
from .casebook import (
    _json_list as _json_list,
)
from .casebook import (
    _parse_json_list as _parse_json_list,
)
from .casebook import (
    _public_bookmark_page as _public_bookmark_page,
)
from .casebook import (
    _stored_bookmark_page as _stored_bookmark_page,
)
from .casebook import (
    delete_casebook_entry as delete_casebook_entry,
)
from .casebook import (
    delete_source_annotation as delete_source_annotation,
)
from .casebook import (
    delete_source_bookmark as delete_source_bookmark,
)
from .casebook import (
    list_casebook_entries as list_casebook_entries,
)
from .casebook import (
    list_source_annotations as list_source_annotations,
)
from .casebook import (
    list_source_bookmarks as list_source_bookmarks,
)
from .casebook import (
    record_casebook_entry as record_casebook_entry,
)
from .casebook import (
    record_source_annotation as record_source_annotation,
)
from .casebook import (
    record_source_bookmark as record_source_bookmark,
)
from .casebook import (
    update_source_annotation as update_source_annotation,
)
from .graph import (
    _GRAPH_ENTITY_TYPES as _GRAPH_ENTITY_TYPES,
)
from .graph import (
    _graph_name_rule_norm as _graph_name_rule_norm,
)
from .graph import (
    _graph_name_rule_value as _graph_name_rule_value,
)
from .graph import (
    delete_graph_name_rule as delete_graph_name_rule,
)
from .graph import (
    doc_entities_extracted as doc_entities_extracted,
)
from .graph import (
    get_graph_review_decision_history as get_graph_review_decision_history,
)
from .graph import (
    get_graph_review_page as get_graph_review_page,
)
from .graph import (
    graph_review_write_transaction as graph_review_write_transaction,
)
from .graph import (
    iter_doc_entities as iter_doc_entities,
)
from .graph import (
    list_graph_name_rules as list_graph_name_rules,
)
from .graph import (
    list_graph_review_decisions as list_graph_review_decisions,
)
from .graph import (
    list_graph_review_runs as list_graph_review_runs,
)
from .graph import (
    list_graph_review_suggestions as list_graph_review_suggestions,
)
from .graph import (
    read_graph_review_snapshot as read_graph_review_snapshot,
)
from .graph import (
    record_doc_entities as record_doc_entities,
)
from .graph import (
    record_graph_review_run as record_graph_review_run,
)
from .graph import (
    reset_orphan_graph_review_decisions as reset_orphan_graph_review_decisions,
)
from .graph import (
    save_graph_name_rule as save_graph_name_rule,
)
from .graph import (
    save_graph_review_decision as save_graph_review_decision,
)
from .graph import (
    save_graph_review_suggestions as save_graph_review_suggestions,
)
from .graph import (
    set_graph_review_suggestion_status as set_graph_review_suggestion_status,
)
from .jobs import (
    _ACTIVE_ADMIN_JOB_STATUSES as _ACTIVE_ADMIN_JOB_STATUSES,
)
from .jobs import (
    _TERMINAL_ADMIN_JOB_STATUSES as _TERMINAL_ADMIN_JOB_STATUSES,
)
from .jobs import (
    _admin_job_active_slot_value as _admin_job_active_slot_value,
)
from .jobs import (
    _finish_admin_job_status_where as _finish_admin_job_status_where,
)
from .jobs import (
    claim_admin_job as claim_admin_job,
)
from .jobs import (
    create_admin_job as create_admin_job,
)
from .jobs import (
    delete_admin_job as delete_admin_job,
)
from .jobs import (
    finish_admin_job as finish_admin_job,
)
from .jobs import (
    get_active_admin_job as get_active_admin_job,
)
from .jobs import (
    get_admin_job as get_admin_job,
)
from .jobs import (
    heartbeat_admin_job as heartbeat_admin_job,
)
from .jobs import (
    list_admin_jobs as list_admin_jobs,
)
from .jobs import (
    mark_admin_job_interrupted as mark_admin_job_interrupted,
)
from .jobs import (
    request_admin_job_cancel as request_admin_job_cancel,
)
from .jobs import (
    update_admin_job_progress as update_admin_job_progress,
)
from .karta import (
    _CANDIDATE_CONFIDENCE as _CANDIDATE_CONFIDENCE,
)
from .karta import (
    _CANDIDATE_PLACE_MATCH as _CANDIDATE_PLACE_MATCH,
)
from .karta import (
    _CANDIDATE_STATUS as _CANDIDATE_STATUS,
)
from .karta import (
    _MAP_CANDIDATE_FIELDS as _MAP_CANDIDATE_FIELDS,
)
from .karta import (
    _MAP_OBSERVATION_FIELDS as _MAP_OBSERVATION_FIELDS,
)
from .karta import (
    _TIME_HH_MM_RE as _TIME_HH_MM_RE,
)
from .karta import (
    _clean_required_text as _clean_required_text,
)
from .karta import (
    _insert_map_observation as _insert_map_observation,
)
from .karta import (
    _row_dict as _row_dict,
)
from .karta import (
    _validate_choice as _validate_choice,
)
from .karta import (
    _validate_lat_lon as _validate_lat_lon,
)
from .karta import (
    _validate_optional_lat_lon as _validate_optional_lat_lon,
)
from .karta import (
    _validate_time_hh_mm as _validate_time_hh_mm,
)
from .karta import (
    approve_map_observation_candidate as approve_map_observation_candidate,
)
from .karta import (
    delete_map_observation as delete_map_observation,
)
from .karta import (
    delete_map_place as delete_map_place,
)
from .karta import (
    list_map_observation_candidates as list_map_observation_candidates,
)
from .karta import (
    list_map_observations as list_map_observations,
)
from .karta import (
    list_map_places as list_map_places,
)
from .karta import (
    map_observation_candidate_exists as map_observation_candidate_exists,
)
from .karta import (
    map_observation_extracted as map_observation_extracted,
)
from .karta import (
    mark_map_observation_extracted as mark_map_observation_extracted,
)
from .karta import (
    record_map_observation as record_map_observation,
)
from .karta import (
    record_map_observation_candidate as record_map_observation_candidate,
)
from .karta import (
    record_map_place as record_map_place,
)
from .karta import (
    reject_map_observation_candidate as reject_map_observation_candidate,
)
from .karta import (
    seed_map_data_if_empty as seed_map_data_if_empty,
)
from .karta import (
    update_map_observation as update_map_observation,
)
from .karta import (
    update_map_observation_candidate as update_map_observation_candidate,
)
from .pipeline import (
    clear_ocr_failures as clear_ocr_failures,
)
from .pipeline import (
    clear_tesseract_blacklisted as clear_tesseract_blacklisted,
)
from .pipeline import (
    clear_tesseract_failed as clear_tesseract_failed,
)
from .pipeline import (
    clear_wpu_decisions as clear_wpu_decisions,
)
from .pipeline import (
    delete_ingest as delete_ingest,
)
from .pipeline import (
    delete_wpu_decisions as delete_wpu_decisions,
)
from .pipeline import (
    files_needing_ingest as files_needing_ingest,
)
from .pipeline import (
    files_needing_normalize as files_needing_normalize,
)
from .pipeline import (
    files_needing_quality as files_needing_quality,
)
from .pipeline import (
    find_download_by_sha1 as find_download_by_sha1,
)
from .pipeline import (
    get_bad_pages as get_bad_pages,
)
from .pipeline import (
    get_ingested_mtime as get_ingested_mtime,
)
from .pipeline import (
    get_pages_for_stem as get_pages_for_stem,
)
from .pipeline import (
    get_pdf_file as get_pdf_file,
)
from .pipeline import (
    is_downloaded as is_downloaded,
)
from .pipeline import (
    is_ocr_fully_failed as is_ocr_fully_failed,
)
from .pipeline import (
    is_tesseract_blacklisted as is_tesseract_blacklisted,
)
from .pipeline import (
    is_tesseract_done as is_tesseract_done,
)
from .pipeline import (
    is_tesseract_failed as is_tesseract_failed,
)
from .pipeline import (
    list_downloads_for_source as list_downloads_for_source,
)
from .pipeline import (
    list_ingested_mtimes as list_ingested_mtimes,
)
from .pipeline import (
    list_low_quality_stems as list_low_quality_stems,
)
from .pipeline import (
    list_ocr_page_texts as list_ocr_page_texts,
)
from .pipeline import (
    list_page_stems as list_page_stems,
)
from .pipeline import (
    list_page_texts_for_stem as list_page_texts_for_stem,
)
from .pipeline import (
    list_pages_matching_text as list_pages_matching_text,
)
from .pipeline import (
    list_pending_redaction_stems as list_pending_redaction_stems,
)
from .pipeline import (
    list_redo_pages as list_redo_pages,
)
from .pipeline import (
    list_surya_fallback_candidates as list_surya_fallback_candidates,
)
from .pipeline import (
    list_tesseract_blacklisted_stems as list_tesseract_blacklisted_stems,
)
from .pipeline import (
    list_tesseract_done_stems as list_tesseract_done_stems,
)
from .pipeline import (
    list_tesseract_failed_stems as list_tesseract_failed_stems,
)
from .pipeline import (
    list_tesseract_skip_stems as list_tesseract_skip_stems,
)
from .pipeline import (
    list_worst_quality as list_worst_quality,
)
from .pipeline import (
    list_wpu_decisions_with_ocr as list_wpu_decisions_with_ocr,
)
from .pipeline import (
    llm_corrected as llm_corrected,
)
from .pipeline import (
    mark_llm_corrected as mark_llm_corrected,
)
from .pipeline import (
    mark_merged as mark_merged,
)
from .pipeline import (
    mark_normalized as mark_normalized,
)
from .pipeline import (
    mark_redaction_checked as mark_redaction_checked,
)
from .pipeline import (
    mark_surya_failed as mark_surya_failed,
)
from .pipeline import (
    mark_tesseract_blacklisted as mark_tesseract_blacklisted,
)
from .pipeline import (
    mark_tesseract_done as mark_tesseract_done,
)
from .pipeline import (
    mark_tesseract_failed as mark_tesseract_failed,
)
from .pipeline import (
    mark_wpu_decided as mark_wpu_decided,
)
from .pipeline import (
    page_exists as page_exists,
)
from .pipeline import (
    record_download as record_download,
)
from .pipeline import (
    record_ingest as record_ingest,
)
from .pipeline import (
    record_page as record_page,
)
from .pipeline import (
    record_quality as record_quality,
)
from .pipeline import (
    record_quality_page as record_quality_page,
)
from .pipeline import (
    redaction_checked as redaction_checked,
)
from .pipeline import (
    reset_pipeline_state_for_stem as reset_pipeline_state_for_stem,
)
from .pipeline import (
    reset_redaction_state as reset_redaction_state,
)
from .pipeline import (
    retry_tesseract_blacklisted as retry_tesseract_blacklisted,
)
from .pipeline import (
    touch_text_mtime as touch_text_mtime,
)
from .pipeline import (
    upsert_pdf_file as upsert_pdf_file,
)
from .pipeline import (
    wpu_decided as wpu_decided,
)
from .usage import (
    _LLM_USAGE_COLUMNS as _LLM_USAGE_COLUMNS,
)
from .usage import (
    add_llm_usage as add_llm_usage,
)
from .usage import (
    delete_llm_usage as delete_llm_usage,
)
from .usage import (
    get_llm_usage as get_llm_usage,
)
from .usage import (
    rename_llm_usage as rename_llm_usage,
)

__all__ = [
    "ActiveAdminJobError",
    "add_llm_usage",
    "approve_map_observation_candidate",
    "claim_admin_job",
    "clear_ocr_failures",
    "clear_tesseract_blacklisted",
    "clear_tesseract_failed",
    "clear_wpu_decisions",
    "connect",
    "create_admin_job",
    "DEFAULT_DB",
    "delete_admin_job",
    "delete_casebook_entry",
    "delete_graph_name_rule",
    "delete_ingest",
    "delete_llm_usage",
    "delete_map_observation",
    "delete_map_place",
    "delete_source_annotation",
    "delete_source_bookmark",
    "delete_wpu_decisions",
    "doc_entities_extracted",
    "DuplicateAdminJobError",
    "files_needing_ingest",
    "files_needing_normalize",
    "files_needing_quality",
    "find_download_by_sha1",
    "finish_admin_job",
    "get_active_admin_job",
    "get_admin_job",
    "get_bad_pages",
    "get_graph_review_decision_history",
    "get_graph_review_page",
    "get_ingested_mtime",
    "get_llm_usage",
    "get_pages_for_stem",
    "get_pdf_file",
    "GRAPH_REVIEW_SCHEMA_SQL",
    "graph_review_write_transaction",
    "heartbeat_admin_job",
    "init_schema",
    "InvalidAdminJobTransition",
    "is_downloaded",
    "is_ocr_fully_failed",
    "is_tesseract_blacklisted",
    "is_tesseract_done",
    "is_tesseract_failed",
    "iter_doc_entities",
    "list_admin_jobs",
    "list_casebook_entries",
    "list_downloads_for_source",
    "list_graph_name_rules",
    "list_graph_review_decisions",
    "list_graph_review_runs",
    "list_graph_review_suggestions",
    "list_ingested_mtimes",
    "list_low_quality_stems",
    "list_map_observation_candidates",
    "list_map_observations",
    "list_map_places",
    "list_ocr_page_texts",
    "list_page_stems",
    "list_page_texts_for_stem",
    "list_pages_matching_text",
    "list_pending_redaction_stems",
    "list_redo_pages",
    "list_source_annotations",
    "list_source_bookmarks",
    "list_surya_fallback_candidates",
    "list_tesseract_blacklisted_stems",
    "list_tesseract_done_stems",
    "list_tesseract_failed_stems",
    "list_tesseract_skip_stems",
    "list_worst_quality",
    "list_wpu_decisions_with_ocr",
    "llm_corrected",
    "LLM_USAGE_SCHEMA_SQL",
    "map_observation_candidate_exists",
    "map_observation_extracted",
    "mark_admin_job_interrupted",
    "mark_llm_corrected",
    "mark_map_observation_extracted",
    "mark_merged",
    "mark_normalized",
    "mark_redaction_checked",
    "mark_surya_failed",
    "mark_tesseract_blacklisted",
    "mark_tesseract_done",
    "mark_tesseract_failed",
    "mark_wpu_decided",
    "MIGRATIONS",
    "now",
    "page_exists",
    "read_graph_review_snapshot",
    "record_casebook_entry",
    "record_doc_entities",
    "record_download",
    "record_graph_review_run",
    "record_ingest",
    "record_map_observation",
    "record_map_observation_candidate",
    "record_map_place",
    "record_page",
    "record_quality",
    "record_quality_page",
    "record_source_annotation",
    "record_source_bookmark",
    "redaction_checked",
    "reject_map_observation_candidate",
    "rename_llm_usage",
    "request_admin_job_cancel",
    "reset_orphan_graph_review_decisions",
    "reset_pipeline_state_for_stem",
    "reset_redaction_state",
    "retry_tesseract_blacklisted",
    "ROOT",
    "save_graph_name_rule",
    "save_graph_review_decision",
    "save_graph_review_suggestions",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "schema_version",
    "seed_map_data_if_empty",
    "set_graph_review_suggestion_status",
    "source_for_path",
    "touch_text_mtime",
    "update_admin_job_progress",
    "update_map_observation",
    "update_map_observation_candidate",
    "update_source_annotation",
    "upsert_pdf_file",
    "wpu_decided",
    "_ACTIVE_ADMIN_JOB_STATUSES",
    "_add_column_if_missing",
    "_admin_job_active_slot_value",
    "_CANDIDATE_CONFIDENCE",
    "_CANDIDATE_PLACE_MATCH",
    "_CANDIDATE_STATUS",
    "_clean_required_text",
    "_column_names",
    "_ensure_schema_version_table",
    "_finish_admin_job_status_where",
    "_GRAPH_ENTITY_TYPES",
    "_graph_name_rule_norm",
    "_graph_name_rule_value",
    "_insert_map_observation",
    "_json_list",
    "_LLM_USAGE_COLUMNS",
    "_MAP_CANDIDATE_FIELDS",
    "_MAP_OBSERVATION_FIELDS",
    "_Migration",
    "_migration_006_pdf_file_ocr_status",
    "_migration_007_admin_jobs",
    "_migration_008_graph_review",
    "_migration_009_graph_name_rules",
    "_migration_010_llm_usage",
    "_parse_json_list",
    "_public_bookmark_page",
    "_record_schema_version",
    "_row_dict",
    "_set_sqlite_user_version",
    "_state_tables",
    "_stored_bookmark_page",
    "_table_exists",
    "_TERMINAL_ADMIN_JOB_STATUSES",
    "_TIME_HH_MM_RE",
    "_validate_choice",
    "_validate_lat_lon",
    "_validate_optional_lat_lon",
    "_validate_time_hh_mm",
]
