import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

WORKFLOW_LOG_FIELDNAME = "workflow_log"
WORKFLOW_LOG_DOCTYPE = "Workflow State Log"


def ensure_workflow_log_field(doc, method=None):
	"""Attach the Workflow Log child table to whichever doctype a Workflow is enabled on."""
	if not doc.is_active:
		return

	document_type = doc.document_type
	if not document_type:
		return

	if frappe.db.exists("Custom Field", {"dt": document_type, "fieldname": WORKFLOW_LOG_FIELDNAME}):
		return

	meta = frappe.get_meta(document_type)
	if meta.get_field(WORKFLOW_LOG_FIELDNAME):
		return

	insert_after = "workflow_state" if meta.get_field("workflow_state") else meta.fields[-1].fieldname

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
			"changed_on": frappe.utils.now_datetime(),
		},
	)
