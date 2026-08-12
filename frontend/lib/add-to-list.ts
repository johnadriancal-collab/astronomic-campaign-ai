// Pure feedback-message logic for AddToListPanel -- kept separate from the
// JSX so the exact wording rules (see the user's own examples: "Added 84
// contacts to X. 5 were already members." and, when every selected contact
// was already a member, stating that plainly rather than as an error) are
// unit-testable without a DOM renderer.

export function describeBulkAddResult(added: number, alreadyMember: number, listName: string): string {
  if (added === 0 && alreadyMember > 0) {
    return alreadyMember === 1
      ? `That contact was already in "${listName}".`
      : `All ${alreadyMember} selected contacts were already in "${listName}".`;
  }
  const addedText = `Added ${added} contact${added === 1 ? "" : "s"} to "${listName}".`;
  return alreadyMember > 0
    ? `${addedText} ${alreadyMember} ${alreadyMember === 1 ? "was" : "were"} already ${alreadyMember === 1 ? "a member" : "members"}.`
    : addedText;
}
