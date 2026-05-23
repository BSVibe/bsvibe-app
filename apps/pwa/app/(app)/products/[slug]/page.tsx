import ProductDetail from "@/components/products/ProductDetail";

/** Product detail route — the focused per-product window. Next 16 / React 19
 *  delivers dynamic-segment `params` as a Promise; await it, then hand the slug
 *  to the client container that loads + renders the view. */
export default async function ProductDetailPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  return <ProductDetail slug={slug} />;
}
