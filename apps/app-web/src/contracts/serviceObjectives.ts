import { isHeadScheduledFromContract } from "./environmentContract.generated";

export const SWEEP_RUN_SOURCE_REGEX_SOURCE = "-[0-9]{8}T[0-9]{4}$";
export const SWEEP_RUN_SOURCE_PATTERN = new RegExp(SWEEP_RUN_SOURCE_REGEX_SOURCE);
export const isHeadScheduled = isHeadScheduledFromContract;
