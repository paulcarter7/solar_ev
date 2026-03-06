#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { SolarEvStack } from "../lib/solar-ev-stack";

const app = new cdk.App();

new SolarEvStack(app, "SolarEvStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? "us-west-2",
  },
  description: "Home Energy Optimizer — solar + EV charging scheduler",
});
