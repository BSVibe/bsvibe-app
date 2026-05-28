import ProductsIndex from "@/components/products/ProductsIndex";

/** Products index route — the full-page overview of every product in the
 *  workspace. The left rail's compact PRODUCTS list and the Brief mobile
 *  embed both link here for the "see / manage everything" surface; bare
 *  `/products` previously 404'd (only `/products/[slug]` existed). */
export default function ProductsIndexPage() {
  return <ProductsIndex />;
}
