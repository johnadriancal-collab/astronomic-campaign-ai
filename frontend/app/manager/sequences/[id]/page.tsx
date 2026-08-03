import { Mail } from "lucide-react";
import { ManagerPlaceholder } from "@/components/manager-placeholder";

export default async function SequenceDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <ManagerPlaceholder
      icon={Mail}
      title="Sequence detail"
      description="Per-step send status and engagement for this sequence isn't implemented yet."
      detail={`Sequence ID: ${id}`}
    />
  );
}
