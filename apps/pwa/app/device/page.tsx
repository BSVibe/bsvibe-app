import { Suspense } from "react";
import { DeviceConsentClient } from "./DeviceConsentClient";

/** RFC 8628 verification page — the `verification_uri` a device prints.
 *
 *  `useSearchParams` requires a Suspense boundary under the app router; the
 *  fallback is deliberately blank so nothing flashes before we know whether
 *  the visitor even has a session. */
export default function DeviceConsentPage() {
  return (
    <Suspense fallback={null}>
      <DeviceConsentClient />
    </Suspense>
  );
}
