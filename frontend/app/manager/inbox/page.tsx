import { Inbox } from "lucide-react";
import { ManagerPlaceholder } from "@/components/manager-placeholder";

export default function InboxPage() {
  return (
    <ManagerPlaceholder
      icon={Inbox}
      title="Inbox"
      description="Replies from leads, unified across all campaigns, will appear here once Inbox is built out."
    />
  );
}
