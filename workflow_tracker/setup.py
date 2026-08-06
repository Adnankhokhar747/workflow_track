import frappe

from workflow_tracker.utils import ensure_workflow_log_field

PO_DOCTYPE = "Purchase Order"
PO_WORKFLOW_NAME = "Purchase Order Approval"

PO_WORKFLOW_STATES = ["Draft", "Pending Approval", "Approved", "Rejected"]
PO_WORKFLOW_ACTIONS = ["Submit for Approval", "Approve", "Reject", "Revise"]


def after_install():
	backfill_existing_workflows()
	_backfill_workflow_log_history_safe()


def _backfill_workflow_log_history_safe():
	"""Wraps backfill_workflow_log_history() so a problem reconstructing history never fails
	the app install/migrate itself - platforms like Frappe Cloud run these hooks with no way
	for the user to intervene or re-run a failed step by hand."""
	try:
		backfill_workflow_log_history()
	except Exception:
		frappe.log_error(title="workflow_tracker: backfill_workflow_log_history failed")


def backfill_existing_workflows():
	"""Attach the Workflow Log child table to every doctype that already has an active Workflow.

	Our hook only fires when a Workflow document is saved, so workflows that were configured
	before this app was installed never trigger it on their own - this backfills those.
	"""
	for name in frappe.get_all("Workflow", filters={"is_active": 1}, pluck="name"):
		ensure_workflow_log_field(frappe.get_doc("Workflow", name))


@frappe.whitelist()
def backfill_workflow_log_history(document_type=None):
	"""Reconstruct Workflow Log rows for documents that changed workflow state before this app
	was installed, using two sources of history Frappe already keeps:

	1. Version (change log) - full from/to state values, but only exists if "Track Changes" was
	   enabled for the doctype at the time.
	2. "Workflow" Comments - apply_workflow() always adds one of these recording the state a
	   document moved *to*, regardless of Track Changes. Used as a fallback wherever Version has
	   nothing, which is what makes history recoverable even for doctypes (e.g. Material Request
	   in stock ERPNext) that don't ship with Track Changes on by default. Since a comment only
	   records the resulting state, from_state is inferred as the previous transition's to_state
	   (or the workflow's first/initial state for the earliest one) - occasionally wrong if a
	   transition was somehow skipped without a comment, but right in the overwhelming majority
	   of cases.

	Runs automatically on `after_install` and on every `after_migrate`, so on a site where
	workflows are already active this happens on its own - no CLI access needed (this is what
	makes it work on platforms like Frappe Cloud, where every app update/deploy runs a migrate
	behind the scenes even without `bench execute` access). It's also safe to re-run manually
	any time via `bench --site <site> execute workflow_tracker.setup.backfill_workflow_log_history`.

	Covers every document with a gap in its history, not just ones that have never been tracked
	at all: a document that transitioned once before this app existed and again afterwards (so it
	already has some live-tracked rows) still gets its earlier, missing transitions filled in -
	only the portion that predates the earliest row already being tracked live. Nothing that's
	already tracked is ever touched, duplicated, or reordered.

	`action` is usually filled in too: neither source records which workflow action caused a
	transition, only the state values - but most workflows have exactly one action per
	(from_state, to_state) pair, so it can be looked up from the Workflow's own transition
	definitions. Left blank only when that mapping is ambiguous (two different actions lead
	through the same pair of states) or the workflow has since changed.

	If a document has neither Version nor Comment history for its workflow state (e.g. it was
	bulk-imported already sitting in its final state, with no recorded journey to get there),
	there is nothing anywhere to recover it from - this is a genuine data limitation, not
	something any amount of reconstruction logic can work around.

	Pass `document_type` to limit the scan to a single doctype; omit to run for every doctype
	that currently has an active Workflow. Returns a per-doctype summary dict.

	Whitelisted (System Manager only) so it can also be triggered on demand via
	`/api/method/workflow_tracker.setup.backfill_workflow_log_history` - useful on platforms
	with no `bench execute` access, without waiting for the next deploy/migrate.
	"""
	frappe.only_for("System Manager")

	workflows = frappe.get_all(
		"Workflow", filters={"is_active": 1}, fields=["name", "document_type", "workflow_state_field"]
	)
	if document_type:
		workflows = [w for w in workflows if w.document_type == document_type]

	summary = {}
	for wf in workflows:
		summary[wf.document_type] = _backfill_history_for_doctype(wf)

	return summary


def _get_workflow_initial_state(workflow_name):
	states = frappe.get_all(
		"Workflow Document State",
		filters={"parenttype": "Workflow", "parent": workflow_name},
		fields=["state"],
		order_by="idx asc",
		limit_page_length=1,
	)
	return states[0].state if states else None


def _build_transition_action_map(doctype):
	"""Best-effort (from_state, to_state) -> action name map, built from the doctype's active
	Workflow transitions. A pair is only included if it unambiguously maps to one action - if two
	transitions share the same from/to states with different actions, history alone can't tell
	them apart, so neither is guessed."""
	workflow_names = frappe.get_all(
		"Workflow", filters={"document_type": doctype, "is_active": 1}, pluck="name"
	)
	if not workflow_names:
		return {}

	transitions = frappe.get_all(
		"Workflow Transition",
		filters={"parenttype": "Workflow", "parent": ["in", workflow_names]},
		fields=["state", "next_state", "action"],
	)

	mapping = {}
	ambiguous = set()
	for t in transitions:
		key = (t.state, t.next_state)
		if key in mapping and mapping[key] != t.action:
			ambiguous.add(key)
		mapping[key] = t.action
	for key in ambiguous:
		mapping.pop(key, None)
	return mapping


def _backfill_history_for_doctype(workflow):
	doctype = workflow.document_type
	state_field = workflow.workflow_state_field

	meta = frappe.get_meta(doctype)
	if not meta.get_field("workflow_log"):
		return {"skipped": "workflow_log field is not attached to this doctype yet"}

	action_map = _build_transition_action_map(doctype)
	initial_state = _get_workflow_initial_state(workflow.name)

	docnames = frappe.get_all(doctype, pluck="name")
	updated = 0
	for docname in docnames:
		if _backfill_history_for_document(doctype, docname, state_field, action_map, initial_state):
			updated += 1

	return {"documents_checked": len(docnames), "documents_updated": updated}


def _version_candidates(doctype, docname, state_field, action_map):
	versions = frappe.get_all(
		"Version",
		filters={"ref_doctype": doctype, "docname": docname},
		fields=["data", "owner", "creation"],
		order_by="creation asc",
	)

	candidates = []
	for v in versions:
		data = frappe.parse_json(v.data) or {}
		for change in data.get("changed") or []:
			fieldname, old_value, new_value = change[0], change[1], change[2]
			if fieldname == state_field and old_value != new_value:
				candidates.append(
					{
						"from_state": old_value,
						"to_state": new_value,
						"action": action_map.get((old_value, new_value), ""),
						"changed_by": v.owner,
						"changed_by_name": frappe.utils.get_fullname(v.owner),
						"changed_on": v.creation,
					}
				)
	return candidates


def _workflow_comment_candidates(doctype, docname, action_map, initial_state):
	"""Fallback for documents/doctypes with no Version history: apply_workflow() always adds a
	Comment (comment_type="Workflow") recording the state it moved to, regardless of Track
	Changes. Only gives the resulting state, so from_state is inferred as the previous
	transition's to_state (or the workflow's initial state for the earliest one)."""
	comments = frappe.get_all(
		"Comment",
		filters={"reference_doctype": doctype, "reference_name": docname, "comment_type": "Workflow"},
		fields=["content", "owner", "creation"],
		order_by="creation asc",
	)

	candidates = []
	previous_state = initial_state
	for c in comments:
		to_state = c.content
		if to_state and to_state != previous_state:
			candidates.append(
				{
					"from_state": previous_state,
					"to_state": to_state,
					"action": action_map.get((previous_state, to_state), ""),
					"changed_by": c.owner,
					"changed_by_name": frappe.utils.get_fullname(c.owner),
					"changed_on": c.creation,
				}
			)
			previous_state = to_state
	return candidates


def _backfill_history_for_document(doctype, docname, state_field, action_map, initial_state):
	existing = frappe.get_all(
		"Workflow State Log",
		filters={"parenttype": doctype, "parent": docname},
		fields=["name", "idx", "changed_on"],
		order_by="idx asc",
	)

	candidates = _version_candidates(doctype, docname, state_field, action_map)
	if not candidates:
		candidates = _workflow_comment_candidates(doctype, docname, action_map, initial_state)

	if not candidates:
		return False

	if not existing:
		missing = candidates
	else:
		# already partly live-tracked (e.g. moved through one state before this app existed,
		# then another after install) - only fill in Version transitions that predate the
		# earliest transition already being tracked live, so nothing live-tracked is duplicated.
		first_live_ts = min(frappe.utils.get_datetime(r.changed_on) for r in existing)
		missing = [c for c in candidates if frappe.utils.get_datetime(c["changed_on"]) < first_live_ts]

	if not missing:
		return False

	for row in existing:
		frappe.db.set_value(
			"Workflow State Log", row.name, "idx", row.idx + len(missing), update_modified=False
		)

	for idx, row in enumerate(missing, start=1):
		child = frappe.new_doc("Workflow State Log")
		child.update(row)
		child.parent = docname
		child.parenttype = doctype
		child.parentfield = "workflow_log"
		child.idx = idx
		child.creation = child.modified = row["changed_on"]
		child.owner = child.modified_by = row["changed_by"]
		child.db_insert()

	return True


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
	return workflow.name
