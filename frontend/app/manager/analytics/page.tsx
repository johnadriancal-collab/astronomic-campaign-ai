import { ChartColumn } from "lucide-react";
import { ManagerPlaceholder } from "@/components/manager-placeholder";

export default function AnalyticsPage() {
  return (
    <ManagerPlaceholder
      icon={ChartColumn}
      title="Analytics"
      description="Send, open, click, and reply performance across campaigns will be reported here once Analytics is built out."
    />
  );
}
