/** True when the firewall is not offered for this server, whatever the stored enabled flag says. */
export function notOffered(view) {
  return view?.available === false || view?.state === 'not_eligible';
}

/** What the on/off switch at the top of the editor shows. Display only: the stored flag is untouched. */
export function switchDisplay(view) {
  if (notOffered(view)) return { checked: false, label: 'Not offered' };
  const checked = !!view?.client_enabled;
  return { checked, label: checked ? 'On' : 'Off' };
}

/** Whether staff may ask for "Apply now"; the server refuses it with 409 not_eligible otherwise. */
export function applyAllowed(view) {
  return !notOffered(view);
}
