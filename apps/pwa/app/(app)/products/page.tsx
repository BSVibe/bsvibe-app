import RailProducts from "@/components/shell/RailProducts";

/** Products index (R18) — the mobile home for the product list. The desktop
 *  left rail carries the same PRODUCTS section, but it's hidden on mobile, so
 *  the bottom-nav "제품" tab lands here. Reuses the rail's product list (load +
 *  list + "New product"); on desktop this route isn't linked (the rail shows
 *  products), so it doesn't reintroduce the removed desktop products tab. */
export default function ProductsPage() {
  return (
    <div className="products-page">
      <RailProducts />
    </div>
  );
}
