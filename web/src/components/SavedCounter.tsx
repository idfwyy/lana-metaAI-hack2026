import { AnimatedNumber } from "./AnimatedNumber";

export function SavedCounter({ amount }: { amount: number }) {
  return (
    <div className="flex items-baseline justify-between gap-3 rounded-[16px] bg-primary-soft px-4 py-3">
      <span className="text-[14px] font-medium text-primary">Saved</span>
      <AnimatedNumber value={amount} className="font-mono text-[24px] font-medium leading-none text-primary" />
    </div>
  );
}
