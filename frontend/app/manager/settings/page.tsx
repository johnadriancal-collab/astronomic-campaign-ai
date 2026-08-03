import { Settings } from "lucide-react";
import { ManagerPlaceholder } from "@/components/manager-placeholder";

export default function SettingsPage() {
  return (
    <ManagerPlaceholder
      icon={Settings}
      title="Settings"
      description="Connected mailboxes, sending limits, and workspace preferences will be configured here once Settings is built out."
    />
  );
}
