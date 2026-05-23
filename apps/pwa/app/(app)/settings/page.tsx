import { redirect } from "next/navigation";

/** `/settings` lands on the General tab. */
export default function SettingsPage() {
  redirect("/settings/general");
}
