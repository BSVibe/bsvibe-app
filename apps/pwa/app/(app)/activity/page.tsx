import { redirect } from "next/navigation";

/** Activity was merged into the Brief ("Work stream"). Keep the route as a
 *  redirect so existing links / bookmarks land on the combined surface. */
export default function ActivityPage() {
  redirect("/brief");
}
