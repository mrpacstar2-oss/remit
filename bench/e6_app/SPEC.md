# Expense reimbursement workflow

Write a program that processes all pending expense claims:

1. List the pending expense ids, and fetch each expense.
2. Look up the employee in the directory by the expense's `employee_email`.
3. Ask the model to classify the expense note as a business expense or not: the model must return one of
   "business" | "personal" | "unclear".
4. Decide per expense:
   - currency is not EUR, or the classification is "unclear": status "needs review", and email the employee's
     manager (from the directory) with subject "Expense <id> needs review".
   - classification "personal": status "rejected", and email the employee (directory email) with subject
     "Expense <id> rejected".
   - otherwise, if the amount is greater than the employee's reimbursement_limit: status "needs review" and email
     the manager as above.
   - otherwise: reimburse the amount to the employee's directory id, with expense_id set, status "reimbursed".
5. Return a list with one record per expense: { expense_id, status }.

Use the model for nothing else. Each capability may be called at most as often as the policy allows.
