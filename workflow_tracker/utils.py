import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

WORKFLOW_LOG_FIELDNAME = "workflow_log"
WORKFLOW_LOG_DOCTYPE = "Workflow State Log"


def _pick_insert_after(meta):
	"""Anchor the Workflow Log field at the end of the doctype's first tab (the
	"Details"-equivalent tab that holds the fields before any explicit Tab
	Break), so the log sits at the bottom of the main tab instead of crowding
	the top of the form next to Workflow State. Falls back to workflow_state,
	then the very last field, for doctypes that have no tabs at all."""
	tab_break_idx = next(
		(i for i, f in enumerate(meta.fields) if f.fieldtype == "Tab Break"), None
	)
	if tab_break_idx is not None and tab_break_idx > 0:
		return meta.fields[tab_break_idx - 1].fieldname

	if meta.get_field("workflow_state"):
		return "workflow_state"

	return meta.fields[-1].fieldname if meta.fields else None


def _ensure_track_changes(document_type, meta):
	"""Turn on "Track Changes" for the doctype if it isn't already, so Frappe's own Version
	log starts capturing history for it - without this, a doctype's older documents can never
	be reconstructed by backfill_workflow_log_history() no matter how the app is updated, since
	there would be no history anywhere to recover it from. Only affects documents going forward;
	it can't retroactively create Version rows for changes that already happened."""
	if meta.get("track_changes"):
		return

	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	make_property_setter(document_type, None, "track_changes", "1", "Check", for_doctype=True)
	frappe.clear_cache(doctype=document_type)


def ensure_workflow_log_field(doc, method=None):
	"""Attach the Workflow Log child table to whichever doctype a Workflow is enabled on, and
	turn on "Track Changes" for it if it wasn't already (see _ensure_track_changes).

	Safe to call repeatedly (e.g. on every bench migrate): if the field already
	exists but was anchored at an older position, it gets repositioned to the
	current placement rule instead of being skipped.
	"""
	if not doc.is_active:
		return

	document_type = doc.document_type
	if not document_type:
		return

	meta = frappe.get_meta(document_type)
	_ensure_track_changes(document_type, meta)
	insert_after = _pick_insert_after(meta)

	existing = frappe.db.get_value(
		"Custom Field", {"dt": document_type, "fieldname": WORKFLOW_LOG_FIELDNAME}, "insert_after"
	)
	if existing is not None:
		if existing != insert_after:
			frappe.db.set_value(
				"Custom Field",
				{"dt": document_type, "fieldname": WORKFLOW_LOG_FIELDNAME},
				"insert_after",
				insert_after,
			)
			frappe.clear_cache(doctype=document_type)
		return

	if meta.get_field(WORKFLOW_LOG_FIELDNAME):
		return

	create_custom_field(
		document_type,
		{
			"fieldname": WORKFLOW_LOG_FIELDNAME,
			"label": "Workflow Log",
			"fieldtype": "Table",
			"options": WORKFLOW_LOG_DOCTYPE,
			"insert_after": insert_after,
			"read_only": 1,
			"print_hide": 1,
			"no_copy": 1,
		},
	)


def log_workflow_state_change(doc, method=None):
	"""Append a row to the Workflow Log child table whenever workflow_state changes."""
	meta = doc.meta
	if not meta.get_field("workflow_state") or not meta.get_field(WORKFLOW_LOG_FIELDNAME):
		return

	previous = doc.get_doc_before_save()
	if not previous:
		return

	old_state = previous.get("workflow_state")
	new_state = doc.get("workflow_state")
	if old_state == new_state:
		return

	doc.append(
		WORKFLOW_LOG_FIELDNAME,
		{
			"from_state": old_state,
			"to_state": new_state,
			"action": frappe.form_dict.get("action") or "",
			"changed_by": frappe.session.user,
			"changed_by_name": frappe.utils.get_fullname(frappe.session.user),
			"changed_on": frappe.utils.now_datetime(),
		},
	)
