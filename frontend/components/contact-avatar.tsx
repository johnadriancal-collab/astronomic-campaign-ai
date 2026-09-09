"use client";

import { useRef, useState } from "react";
import { Camera, Loader2 } from "lucide-react";
import { ApiError, uploadCrmContactPhoto, type CrmContact } from "@/lib/api";
import { getContactInitials } from "@/lib/contact-avatar";
import { cn } from "@/lib/utils";

/**
 * Contact avatar display + manual upload/replace (Profile Photos Stage 1).
 * Falls back to an initials circle when profile_photo_url is null, or if the
 * stored URL ever fails to load (e.g. before real object storage is
 * provisioned -- see ProfilePhotoService's own note that profile_photo_url
 * is null whenever no CDN base URL is configured yet). Deliberately scoped
 * to the Contact detail page only for this stage -- NOT used in the main
 * Contacts table yet (see the approved Stage 1 plan).
 */
export function ContactAvatar({
  contact,
  onUploaded,
}: {
  contact: CrmContact;
  onUploaded: (updated: CrmContact) => void;
}) {
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [imageFailed, setImageFailed] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const initials = getContactInitials(contact.first_name, contact.last_name, contact.email);
  const showPhoto = Boolean(contact.profile_photo_url) && !imageFailed;

  async function handleFileSelected(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (!file) return;

    setUploading(true);
    setError(null);
    try {
      const updated = await uploadCrmContactPhoto(contact.crm_contact_id, file);
      setImageFailed(false);
      onUploaded(updated);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed. Please try again.");
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="flex flex-col items-center gap-1.5">
      <button
        type="button"
        onClick={() => fileInputRef.current?.click()}
        disabled={uploading}
        className={cn(
          "group relative flex h-20 w-20 shrink-0 items-center justify-center overflow-hidden rounded-full border bg-muted text-lg font-medium text-muted-foreground transition-opacity",
          uploading && "opacity-60"
        )}
        title={contact.profile_photo_url ? "Replace photo" : "Upload photo"}
      >
        {showPhoto ? (
          // eslint-disable-next-line @next/next/no-img-element -- an external/CDN-hosted photo, not a build-time local asset
          <img
            src={contact.profile_photo_url ?? undefined}
            alt=""
            className="h-full w-full object-cover"
            onError={() => setImageFailed(true)}
          />
        ) : (
          <span>{initials}</span>
        )}

        <span className="absolute inset-0 flex items-center justify-center bg-black/0 opacity-0 transition-all group-hover:bg-black/40 group-hover:opacity-100">
          {uploading ? <Loader2 className="h-5 w-5 animate-spin text-white" /> : <Camera className="h-5 w-5 text-white" />}
        </span>
      </button>

      <input ref={fileInputRef} type="file" accept="image/jpeg,image/png,image/webp" className="hidden" onChange={handleFileSelected} />

      {error && <p className="max-w-40 text-center text-xs text-destructive">{error}</p>}
    </div>
  );
}
