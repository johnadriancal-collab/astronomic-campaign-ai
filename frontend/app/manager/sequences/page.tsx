import { Mail } from "lucide-react";
import { ManagerPlaceholder } from "@/components/manager-placeholder";

export default function SequencesPage() {
  return (
    <ManagerPlaceholder
      icon={Mail}
      title="Sequences / Emails"
      description="The email sequences deployed to Apollo for each campaign will be listed here, alongside their send status."
    />
  );
}
