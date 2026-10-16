
# Lesson 2: Conditions and Loops

stock = 3
minimum_stock = 5

# 1. Conditions
if stock == 0:
    print("Out of stock")
elif stock <= minimum_stock:
    print("Low stock")
else:
    print("Stock level is healthy")

# 2. For loop
products = ["Ice Machine K40", "Ice Machine K60", "Ice Machine K125", "Ice Machine K200 "]

print("\nProduct List:")

for product in products:
    print(product)

# 3. While loop
count = 1

print("\nCounting:")

while count <= 5:
    print(count)
    count += 1
