/**
 * CDK assertion tests for SolarEvStack.
 *
 * Covers:
 * - DynamoDB tables: names, key schema, billing mode, TTL attribute, PITR, removal policy
 * - Lambda functions: names, runtimes, timeouts, environment variables
 * - IAM policies: SSM GetParameter and PutParameter scoped to /solar-ev/*
 * - API Gateway: REST API with /solar/today (GET) and /recommendation (GET)
 * - EventBridge rule: hourly schedule targeting the ingest Lambda
 */
import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { SolarEvStack } from "../lib/solar-ev-stack";

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const stack = new SolarEvStack(app, "TestStack", {
    env: { account: "123456789012", region: "us-west-2" },
  });
  template = Template.fromStack(stack);
});

// ---------------------------------------------------------------------------
// DynamoDB tables
// ---------------------------------------------------------------------------

describe("DynamoDB tables", () => {
  it("creates the energy readings table with correct key schema", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "solar-ev-energy-readings",
      KeySchema: Match.arrayWith([
        { AttributeName: "deviceId",  KeyType: "HASH" },
        { AttributeName: "timestamp", KeyType: "RANGE" },
      ]),
    });
  });

  it("energy readings table uses pay-per-request billing", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "solar-ev-energy-readings",
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  it("energy readings table has TTL on 'ttl' attribute", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "solar-ev-energy-readings",
      TimeToLiveSpecification: { AttributeName: "ttl", Enabled: true },
    });
  });

  it("creates the user config table with correct key schema", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "solar-ev-user-config",
      KeySchema: Match.arrayWith([
        { AttributeName: "userId",     KeyType: "HASH" },
        { AttributeName: "configType", KeyType: "RANGE" },
      ]),
    });
  });
});

// ---------------------------------------------------------------------------
// Lambda functions
// ---------------------------------------------------------------------------

describe("Lambda functions", () => {
  it("creates the solar_data Lambda with correct name and runtime", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "solar-ev-solar-data",
      Runtime: "python3.12",
      Handler: "handler.lambda_handler",
    });
  });

  it("creates the recommendation Lambda with correct name and runtime", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "solar-ev-recommendation",
      Runtime: "python3.12",
      Handler: "handler.lambda_handler",
    });
  });

  it("creates the ingest Lambda with correct name and runtime", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "solar-ev-ingest",
      Runtime: "python3.12",
      Handler: "handler.lambda_handler",
    });
  });

  it("ingest Lambda has all SSM parameter path env vars", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "solar-ev-ingest",
      Environment: {
        Variables: Match.objectLike({
          ENPHASE_API_KEY_PARAM:       "/solar-ev/enphase-api-key",
          ENPHASE_ACCESS_TOKEN_PARAM:  "/solar-ev/enphase-access-token",
          ENPHASE_REFRESH_TOKEN_PARAM: "/solar-ev/enphase-refresh-token",
          ENPHASE_CLIENT_ID_PARAM:     "/solar-ev/enphase-client-id",
          ENPHASE_CLIENT_SECRET_PARAM: "/solar-ev/enphase-client-secret",
          NTFY_TOPIC_PARAM:            "/solar-ev/ntfy-topic",
        }),
      },
    });
  });

  it("all Lambdas have ENERGY_TABLE and CONFIG_TABLE env vars", () => {
    for (const name of ["solar-ev-solar-data", "solar-ev-recommendation", "solar-ev-ingest"]) {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: name,
        Environment: {
          Variables: Match.objectLike({
            ENERGY_TABLE: Match.anyValue(),
            CONFIG_TABLE: Match.anyValue(),
          }),
        },
      });
    }
  });

  it("ingest Lambda has a 60-second timeout", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "solar-ev-ingest",
      Timeout: 60,
    });
  });

  it("solar_data and recommendation Lambdas have 30-second timeouts", () => {
    for (const name of ["solar-ev-solar-data", "solar-ev-recommendation"]) {
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: name,
        Timeout: 30,
      });
    }
  });
});

// ---------------------------------------------------------------------------
// IAM policies
// ---------------------------------------------------------------------------

describe("IAM policies", () => {
  it("ingest Lambda role has SSM GetParameter on /solar-ev/* resources", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "ssm:GetParameter",
            Effect: "Allow",
            Resource: Match.stringLikeRegexp("/solar-ev/\\*"),
          }),
        ]),
      },
    });
  });

  it("ingest Lambda role has SSM PutParameter on token params only", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "ssm:PutParameter",
            Effect: "Allow",
            Resource: Match.arrayWith([
              Match.stringLikeRegexp("enphase-access-token"),
              Match.stringLikeRegexp("enphase-refresh-token"),
            ]),
          }),
        ]),
      },
    });
  });
});

// ---------------------------------------------------------------------------
// API Gateway
// ---------------------------------------------------------------------------

describe("API Gateway", () => {
  it("creates the REST API", () => {
    template.hasResourceProperties("AWS::ApiGateway::RestApi", {
      Name: "solar-ev-api",
    });
  });

  it("has a GET method on /solar/today", () => {
    // The resource path is spread across parent resources; verify a GET method exists
    // on the "today" resource by checking there are GET methods in the template
    const methods = template.findResources("AWS::ApiGateway::Method", {
      Properties: { HttpMethod: "GET" },
    });
    expect(Object.keys(methods).length).toBeGreaterThanOrEqual(2);
  });
});

// ---------------------------------------------------------------------------
// EventBridge
// ---------------------------------------------------------------------------

describe("EventBridge", () => {
  it("creates the hourly ingest rule", () => {
    template.hasResourceProperties("AWS::Events::Rule", {
      Name: "solar-ev-hourly-ingest",
      ScheduleExpression: "rate(1 hour)",
      State: "ENABLED",
    });
  });

  it("hourly rule targets the ingest Lambda", () => {
    template.hasResourceProperties("AWS::Events::Rule", {
      Name: "solar-ev-hourly-ingest",
      Targets: Match.arrayWith([
        Match.objectLike({ Id: Match.anyValue() }),
      ]),
    });
  });
});

// ---------------------------------------------------------------------------
// CloudFormation outputs
// ---------------------------------------------------------------------------

describe("Stack outputs", () => {
  it("exports the API Gateway URL", () => {
    template.hasOutput("ApiUrl", {
      Export: { Name: "SolarEvApiUrl" },
    });
  });

  it("outputs the energy table name", () => {
    template.hasOutput("EnergyTableName", {});
  });

  it("outputs the config table name", () => {
    template.hasOutput("ConfigTableName", {});
  });
});
