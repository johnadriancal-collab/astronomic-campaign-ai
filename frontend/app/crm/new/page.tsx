"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, createCrmContact } from "@/lib/api";

const FIELDS: [string, string][] = [
  ["first_name", "First name"],
  ["last_name", "Last name"],
  ["email", "Email"],
  ["phone", "Phone"],
  ["linkedin_url", "LinkedIn URL"],
  ["title", "Title"],
  ["company", "Company"],
  ["city", "City"],
  ["state", "State"],
  ["country", "Country"],
];

export default function NewCrmContactPage() {
  const router = useRouter();
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const fields = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim() !== ""));
      const contact = await createCrmContact(fields);
      router.push(`/crm/${contact.crm_contact_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't create contact (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mx-auto max-w-xl px-6 py-10">
      <button
        onClick={() => router.push("/crm")}
        className="mb-6 flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        All contacts
      </button>

      <h1 className="mb-2 text-2xl font-semibold tracking-tight">New CRM contact</h1>
      <p className="mb-6 text-sm text-muted-foreground">
        Investor Thesis fields and custom fields can be filled in on the contact&apos;s page after creation.
      </p>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTitle>Couldn&apos;t create contact</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Profile</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="grid gap-3 sm:grid-cols-2">
            {FIELDS.map(([key, label]) => (
              <div key={key} className="space-y-1">
                <label className="text-xs font-medium text-muted-foreground">{label}</label>
                <Input
                  value={values[key] ?? ""}
                  onChange={(e) => setValues((prev) => ({ ...prev, [key]: e.target.value }))}
                />
              </div>
            ))}
            <div className="sm:col-span-2 mt-2">
              <Button type="submit" disabled={saving} className="w-full">
                {saving ? "Creating..." : "Create contact"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
