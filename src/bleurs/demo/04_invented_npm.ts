// The same failure in TypeScript, where the supply-chain stakes are higher.
import { z } from "zod";
import { useDebounce } from "react-hooks-utils-toolkit";
import { formatCurrency } from "@acme/intl-format-helpers";

export const Schema = z.object({ amount: z.number() });

export function Price({ amount }: { amount: number }) {
  const value = useDebounce(amount, 200);
  return formatCurrency(value, "USD");
}
