import frappe

from workflow_tracker.utils import ensure_workflow_log_field

PO_DOCTYPE = "Purchase Order"
PO_WORKFLOW_NAME = "Purchase Order Approval"

PO_WORKFLOW_STATES = ["Draft", "Pending Approval", "Approved", "Rejected"]
PO_WORKFLOW_ACTIONS = ["Submit for Approval", "Approve", "Reject", "Revise"]


def after_install():
	backfill_existing_workflows()


def backfill_existing_workflows():
	"""Attach the Workflow Log child table to every doctype that already has an active Workflow.

	Our hook only fires when a Workflow document is saved, so workflows that were configured
	before this app was installed never trigger it on their own - this backfills those.
	"""
	for name in frappe.get_all("Workflow", filters={"is_active": 1}, pluck="name"):
		ensure_workflow_log_field(frappe.get_doc("Workflow", name))


def _ensure_workflow_state(state):
	if not frappe.db.exists("Workflow State", state):
		frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state}).insert(
			ignore_permissions=True
		)


def _ensure_workflow_action(action):
	if not frappe.db.exists("Workflow Action Master", action):
		frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert(
			ignore_permissions=True
		)


def setup_po_approval_workflow():
	"""Create (or update) a Draft -> Pending Approval -> Approved/Rejected workflow for Purchase Order.

	Safe to run more than once. Requires ERPNext (the Purchase Order doctype) to be installed.
	Approving submits the Purchase Order; rejecting keeps it as an editable draft that can be
	revised and resubmitted for approval.
	"""
	if not frappe.db.exists("DocType", PO_DOCTYPE):
		frappe.throw(f"'{PO_DOCTYPE}' doctype not found - is ERPNext installed on this site?")

	for state in PO_WORKFLOW_STATES:
		_ensure_workflow_state(state)
	for action in PO_WORKFLOW_ACTIONS:
		_ensure_workflow_action(action)

	states = [
		{"state": "Draft", "doc_status": "0", "allow_edit": "Purchase User"},
		{"state": "Pending Approval", "doc_status": "0", "allow_edit": "Purchase Manager"},
		{"state": "Approved", "doc_status": "1", "allow_edit": "Purchase Manager"},
		{"state": "Rejected", "doc_status": "0", "allow_edit": "Purchase User"},
	]
	transitions = [
		{
			"state": "Draft",
			"action": "Submit for Approval",
			"next_state": "Pending Approval",
			"allowed": "Purchase User",
		},
		{
			"state": "Pending Approval",
			"action": "Approve",
			"next_state": "Approved",
			"allowed": "Purchase Manager",
		},
		{
			"state": "Pending Approval",
			"action": "Reject",
			"next_state": "Rejected",
			"allowed": "Purchase Manager",
		},
		{
			"state": "Rejected",
			"action": "Revise",
			"next_state": "Draft",
			"allowed": "Purchase User",
		},
	]

	if frappe.db.exists("Workflow", PO_WORKFLOW_NAME):
		workflow = frappe.get_doc("Workflow", PO_WORKFLOW_NAME)
		workflow.set("states", states)
		workflow.set("transitions", transitions)
	else:
		workflow = frappe.get_doc(
			{
				"doctype": "Workflow",
				"workflow_name": PO_WORKFLOW_NAME,
				"document_type": PO_DOCTYPE,
				"workflow_state_field": "workflow_state",
				"send_email_alert": 0,
				"states": states,
				"transitions": transitions,
			}
		)

	workflow.is_active = 1
	workflow.save(ignore_permissions=True)
	frappe.db.commit()
	return workflow.name
