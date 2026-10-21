import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

WORKFLOW_LOG_FIELDNAME = "workflow_log"
WORKFLOW_LOG_SECTION = "workflow_log_section"
WORKFLOW_LOG_DOCTYPE = "Workflow State Log"
_WORKFLOW_LOG_FIELDS = {WORKFLOW_LOG_FIELDNAME, WORKFLOW_LOG_SECTION}


def _content_fields(meta):
	"""DocType fields excluding the Workflow Log section and table themselves."""
	return [f for f in meta.fields if f.fieldname not in _WORKFLOW_LOG_FIELDS]


def _pick_insert_after(meta):
	"""Field the Workflow Log section should follow.

	That is the last field of the first tab, so the log is a full-width section
	at the bottom of the main form. A doctype with no tabs uses its last field.
	Never anchor on workflow_state: that field sits in the header, and pinning
	the log there crams the table into the top column.
	"""
	fields = _content_fields(meta)
	if not fields:
		return None

	tab_indexes = [i for i, f in enumerate(fields) if f.fieldtype == "Tab Break"]
	if not tab_indexes:
		return fields[-1].fieldname

	# Fields before the first Tab Break are an implicit first tab.
	if tab_indexes[0] > 0:
		return fields[tab_indexes[0] - 1].fieldname

	# The first field is already a Tab Break. End that tab at the next one.
	if len(tab_indexes) >= 2:
		return fields[tab_indexes[1] - 1].fieldname

	return fields[-1].fieldname


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

	Safe to call repeatedly (e.g. on every bench migrate): if the section or the
	table already exists but was anchored at an older position, both are moved
	to the current placement rule instead of being skipped.
	"""
	if not doc.is_active:
		return

	document_type = doc.document_type
	if not document_type:
		return

	meta = frappe.get_meta(document_type)
	_ensure_track_changes(document_type, meta)
	anchor = _pick_insert_after(meta)
	if not anchor:
		return

	_ensure_section(document_type, anchor)
	_ensure_table(document_type)


def _custom_field_name(document_type, fieldname):
	return frappe.db.get_value("Custom Field", {"dt": document_type, "fieldname": fieldname}, "name")


def _move_custom_field(document_type, fieldname, insert_after):
	"""Point an existing custom field at insert_after. Returns whether the field exists."""
	name = _custom_field_name(document_type, fieldname)
	if not name:
		return False

	if frappe.db.get_value("Custom Field", name, "insert_after") != insert_after:
		frappe.db.set_value("Custom Field", name, "insert_after", insert_after)
		frappe.clear_cache(doctype=document_type)
	return True


def _ensure_section(document_type, anchor):
	"""Full-width section so the log is not stuck inside the header column."""
	if _move_custom_field(document_type, WORKFLOW_LOG_SECTION, anchor):
		return

	if frappe.get_meta(document_type).get_field(WORKFLOW_LOG_SECTION):
		return

	create_custom_field(
		document_type,
		{
			"fieldname": WORKFLOW_LOG_SECTION,
			"label": "Workflow Log",
			"fieldtype": "Section Break",
			"insert_after": anchor,
		},
	)
	frappe.clear_cache(doctype=document_type)


def _ensure_table(document_type):
	if _move_custom_field(document_type, WORKFLOW_LOG_FIELDNAME, WORKFLOW_LOG_SECTION):
		return

	if frappe.get_meta(document_type).get_field(WORKFLOW_LOG_FIELDNAME):
		return

	create_custom_field(
		document_type,
		{
			"fieldname": WORKFLOW_LOG_FIELDNAME,
			"label": "Workflow Log",
			"fieldtype": "Table",
			"options": WORKFLOW_LOG_DOCTYPE,
			"insert_after": WORKFLOW_LOG_SECTION,
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
